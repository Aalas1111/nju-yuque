"""常驻形态：**webhook 接收 + 轮询兜底**。

两种触发方式互补，缺一不可：

- **webhook**（语雀知识库 → 设置 → 开发者/消息推送）：实时，但要求接收端**公网可达**，
  且语雀侧会重试/丢事件，不能当唯一真相；
- **轮询**（默认 60s）：兜底，也是**唯一能可靠发现「文档被删除」**的手段。

实现上只有一个工作线程：webhook 处理器只把事件塞进队列并立刻返回 200，
真正的处理（读知识库 + 判定 + 落盘）全部在这个线程里串行做，避免并发写 ``state.json``。

``parse_webhook`` 对语雀的报文做**宽容解析**：语雀没有公开的 webhook 报文文档，
不同事件（发布/更新/删除/评论）字段名不完全一致，所以：
① 只认关键字段（动作 + 文档 id），认不出来就当 ``unknown`` 丢给轮询兜底；
② ``--dump-webhook <dir>`` 可以把原始报文落盘，方便对照真实字段后收紧解析。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .pipeline import Pipeline, RoundReport
from .store import Store

WEBHOOK_PATH = "/yuque/webhook"
MAX_BODY_BYTES = 1 << 20  # webhook 报文上限 1 MiB（超了直接 413，别把内存吃光）
REQUEST_TIMEOUT = 10  # 每个连接的读写超时（秒），防止慢连接占满线程
SECRET_HEADERS = ("X-Yuque-Token", "X-Webhook-Token", "X-Yuque-Signature")
# 只把「真删除」当删除事件；remove（移出目录）/unpublish（取消发布）都交给轮询按台账确认，
# 否则会发「删除 ≠ 撤回」的误报。
DELETE_HINTS = ("delete", "destroy", "trash")
UPSERT_HINTS = ("publish", "update", "create", "save", "edit", "new", "content")
# unpublish（取消发布）既不是删除也不是更新：它不影响申请，交给轮询兜底即可
IGNORE_HINTS = ("comment", "reply", "ping", "test", "unpublish")

ACTION_UPSERT = "upsert"
ACTION_DELETE = "delete"
ACTION_IGNORE = "ignore"
ACTION_UNKNOWN = "unknown"


@dataclass
class WebhookEvent:
    """从语雀 webhook 报文里抽出来的最小事件。"""

    action: str = ACTION_UNKNOWN
    doc_id: int = 0
    title: str = ""
    slug: str = ""
    repo: str = ""
    action_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """能定位到一篇文档才算可用（否则交给轮询）。"""
        return self.doc_id > 0 and self.action in {ACTION_UPSERT, ACTION_DELETE}


# ---------------------------------------------------------------- 报文解析
def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_webhook(payload: dict[str, Any]) -> WebhookEvent:
    """把语雀 webhook 报文解析成 :class:`WebhookEvent`（尽量宽容）。"""
    raw = payload if isinstance(payload, dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    book = data.get("book") if isinstance(data.get("book"), dict) else {}

    action_type = str(
        _pick(data, "action_type", "action", "event", "type")
        or _pick(raw, "action_type", "action", "event", "type")
        or ""
    ).lower()
    lowered = action_type.replace("-", "_")
    if any(hint in lowered for hint in IGNORE_HINTS):
        action = ACTION_IGNORE
    elif any(hint in lowered for hint in DELETE_HINTS):
        action = ACTION_DELETE
    elif any(hint in lowered for hint in UPSERT_HINTS):
        action = ACTION_UPSERT
    else:
        action = ACTION_UNKNOWN

    doc_id = _as_int(_pick(data, "doc_id", "id", "target_id") or _pick(target, "id", "doc_id"))
    title = str(_pick(data, "title", "name") or _pick(target, "title") or "")
    slug = str(_pick(data, "slug", "path") or _pick(target, "slug") or "")
    repo = str(
        _pick(book, "namespace", "slug", "name") or _pick(data, "namespace", "book_namespace") or ""
    )
    return WebhookEvent(
        action=action,
        doc_id=doc_id,
        title=title,
        slug=slug,
        repo=repo,
        action_type=action_type,
        raw=raw,
    )


# ---------------------------------------------------------------- HTTP 服务
def make_handler(
    on_event: Callable[[WebhookEvent], None],
    *,
    path: str = WEBHOOK_PATH,
    secret: str = "",
    dump_dir: Path | None = None,
    log: Callable[[str], None] = print,
    store: Store | None = None,
) -> type[BaseHTTPRequestHandler]:
    """造一个 handler 类：``POST <path>`` 收事件，``GET /healthz`` 探活。"""

    def _authorized(handler: BaseHTTPRequestHandler) -> bool:
        if not secret:
            return True
        query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if params.get("token") == secret or params.get("secret") == secret:
            return True
        for header in SECRET_HEADERS:
            if handler.headers.get(header) == secret:
                return True
        auth = handler.headers.get("Authorization") or ""
        return auth.removeprefix("Bearer ").strip() == secret

    class _Handler(BaseHTTPRequestHandler):
        server_version = "yuque-classroom/1.0"
        # StreamRequestHandler.timeout：慢连接最多占 REQUEST_TIMEOUT 秒
        timeout = REQUEST_TIMEOUT

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            if self.path.split("?", 1)[0].rstrip("/") not in ("/healthz", "/health"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            counts = store.counts() if store else {}
            self._send(200, {"ok": True, "repo": store.repo if store else "", "counts": counts})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0].rstrip("/") != path.rstrip("/"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            if not _authorized(self):
                self._send(401, {"ok": False, "error": "bad token"})
                return
            length = _as_int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": "payload too large"})
                return
            body = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                payload = {"_raw": body.decode("utf-8", "replace")}
            if dump_dir is not None:
                try:
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    # 文件名一律自己生成：报文里的任何字段都是外部输入，不能拿来拼路径
                    name = f"webhook-{time.time_ns()}-{uuid.uuid4().hex[:6]}.json"
                    (dump_dir / name).write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError as exc:  # 落盘失败不能影响接收
                    log(f"! webhook 报文落盘失败：{exc}")
            event = parse_webhook(payload)
            on_event(event)
            # 必须快速返回（语雀侧约 3s 超时），真正的处理在队列里异步做
            self._send(200, {"ok": True, "action": event.action, "doc_id": event.doc_id})

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log("[webhook] " + (fmt % args))

    return _Handler


def make_server(
    *,
    host: str,
    port: int,
    handler: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------- 常驻循环
class Runner:
    """轮询 + webhook 的单线程常驻循环。"""

    def __init__(
        self,
        pipeline: Pipeline,
        store: Store,
        *,
        interval: float = 60.0,
        host: str = "",
        port: int = 0,
        path: str = WEBHOOK_PATH,
        secret: str = "",
        dump_dir: Path | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.pipeline = pipeline
        self.store = store
        self.interval = max(1.0, float(interval))
        self.host = host
        self.port = port
        self.path = path
        self.secret = secret
        self.dump_dir = dump_dir
        self.log = log
        self.events: queue.Queue[WebhookEvent] = queue.Queue()
        self._stop = threading.Event()
        self.server: ThreadingHTTPServer | None = None

    # -- 单轮 -------------------------------------------------------------
    def tick(self, force: set[int] | None = None) -> RoundReport:
        """跑一轮并把结果打到日志。"""
        report = self.pipeline.run_once(force=force or set())
        if report.errors:
            for err in report.errors:
                self.log(f"! {err}")
        if not report.snapshot_ok:
            return report
        self.log(
            f"[{report.at}] {report.summary()}｜知识库 {report.seen_docs} 篇"
            f"｜跳过 {dict(report.skipped)}"
        )
        for note in report.notes:
            self.log(f"  · {note}")
        for app_id in report.accepted:
            self.log(f"  ✅ 已受理 {app_id}")
        for title in report.rejected:
            self.log(f"  ↩️ 退回 {title}")
        for title in report.unrecognized:
            self.log(f"  ❓ 无法识别 {title}")
        for title in report.tampered:
            self.log(f"  🔒 改动告警 {title}")
        for title in report.deleted:
            self.log(f"  🗑️ 文档消失 {title}")
        return report

    def belongs_here(self, event: WebhookEvent) -> bool:
        """报文里的知识库要跟本进程处理的一致（防止把别的库的推送也当自己的）。"""
        repo = (event.repo or "").strip().strip("/")
        if not repo:
            return True
        mine = self.pipeline.opts.repo.strip().strip("/")
        if repo == mine:
            return True
        return repo.split("/")[-1] == mine.split("/")[-1]

    def serve_background(self) -> ThreadingHTTPServer | None:
        """起 webhook 监听线程（``port`` 为 0 表示不监听）。"""
        if not self.port:
            return None
        handler = make_handler(
            self.events.put,
            path=self.path,
            secret=self.secret,
            dump_dir=self.dump_dir,
            log=self.log,
            store=self.store,
        )
        try:
            self.server = make_server(host=self.host or "0.0.0.0", port=self.port, handler=handler)
        except OSError as exc:
            # 端口被占用之类：降级成「只有轮询」，但日志要足够响
            self.log(f"! webhook 监听失败（{exc}），本次只做轮询兜底；修好后重启即可")
            self.server = None
            return None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.log(f"[webhook] 监听 http://{self.host or '0.0.0.0'}:{self.port}{self.path}")
        if not self.secret:
            self.log("! 未设置 --secret：任何能访问该端口的人都能伪造成语雀事件，强烈建议设置")
        return self.server

    def _drain(self, timeout: float) -> list[WebhookEvent]:
        """等第一个事件（最多 ``timeout`` 秒），然后把手头的事件一次拿光。"""
        try:
            first = self.events.get(timeout=timeout)
        except queue.Empty:
            return []
        out = [first]
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def run_forever(self) -> None:
        """阻塞运行：轮询 + webhook。Ctrl-C 优雅退出。"""
        try:
            self.serve_background()
        except Exception as exc:  # 起服务失败也要能继续轮询
            self.log(f"! webhook 服务起不来（{type(exc).__name__}: {exc}），只做轮询")
        self.log(f"[agent] 开始常驻：repo={self.pipeline.opts.repo} 轮询间隔={self.interval:.0f}s")
        while not self._stop.is_set():
            events = self._drain(self.interval)
            if self._stop.is_set():  # stop() 会塞一个哨兵事件把 _drain 叫醒
                break
            # 事件处理与整轮处理都兜住异常：常驻进程不能被任何一轮打死
            # （磁盘满、网络抖、notifier 报错、webhook 报文畸形……）
            try:
                force = self.handle_events(events)
                self.tick(force=force)
            except Exception as exc:
                self.log(f"! 本轮处理异常，已跳过：{type(exc).__name__}: {exc}")

    def handle_events(self, events: list[WebhookEvent]) -> set[int]:
        """处理一批 webhook 事件，返回「要立刻重读的 doc_id」集合。

        删除事件即时处理（权威）；更新事件只定向重读那一篇（快）；
        别的事件/认不出来的交给接下来的整轮轮询兜底。
        """
        force: set[int] = set()
        for event in events:
            if not self.belongs_here(event):
                self.log(f"  ？（webhook）事件来自别的知识库（{event.repo}），已忽略")
                continue
            if event.action == ACTION_DELETE and event.doc_id:
                notice = self.pipeline.handle_delete(event.doc_id)
                if notice is not None:
                    self.log(f"  🗑️（webhook）{notice.doc.title} 文档已删除 → 已通知")
                elif self.pipeline.store.get(event.doc_id) is not None:
                    self.log(
                        f"  ？（webhook）说删了但 #{event.doc_id} 仍读得到 → 忽略"
                        "（不改本地记录、不发消息）"
                    )
            elif event.action == ACTION_UPSERT and event.doc_id:
                force.add(event.doc_id)
            elif event.action == ACTION_UNKNOWN:
                self.log(
                    f"  ？（webhook）无法解析的事件：{event.action_type or '未知'}（已交给轮询）"
                )
        return force

    def stop(self) -> None:
        """停掉常驻循环与 webhook 服务（幂等，可从信号处理里调）。"""
        self._stop.set()
        self.events.put(WebhookEvent(action=ACTION_IGNORE))  # 把阻塞在 _drain 的循环叫醒
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
