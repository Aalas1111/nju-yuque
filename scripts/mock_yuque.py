"""本地「仿真语雀」服务：把官方 OpenAPI 的读写接口在本地实现一遍。

用途：**在没有语雀凭证 / 不想动真实知识库的情况下，把 classroom agent 完整跑起来**
（真 CLI + 真 HTTP + 真 webhook + 真 outbox + 真持久化），也方便修 bug 时反复复现。

实现范围（够 agent 用，也够模拟「社员在网页里写文档」）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v2/hello` | 探活 |
| GET | `/api/v2/user` | 团队令牌会返回 `{type: Group, login}` |
| GET | `/api/v2/repos/{repo}/toc` | 目录（含 TITLE / DOC） |
| PUT | `/api/v2/repos/{repo}/toc` | appendNode（挂文档 / 建分组） |
| GET | `/api/v2/repos/{repo}/docs` | 文档列表（`limit`/`offset`） |
| GET | `/api/v2/repos/{repo}/docs/<id 或 slug>?raw=1` | 文档详情（含 `body`、`creator`） |
| POST | `/api/v2/repos/{repo}/docs` | 新建文档（社员在网页里新建） |
| PUT | `/api/v2/repos/{repo}/docs/{id}` | 改标题 / 改正文 |
| DELETE | `/api/v2/repos/{repo}/docs/{id}` | 删除文档 |

约定：一律要 `X-Auth-Token`（否则 401），响应统一包成 `{"data": ...}`，
并回 `x-oauth-scopes` 头（和真实服务一致，方便 `can_write` 判断）。

用法::

    python scripts/mock_yuque.py --port 8799 --token local-dev-token --seed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

DEFAULT_REPO = "lqogh0/jsjysq"
SCOPES = "group:read,repo:read,doc,repo,statistic:read"

DOCS_RE = re.compile(r"^/api/v2/repos/(?P<repo>[^/]+/[^/]+)/docs(?:/(?P<key>[^/]+))?$")
TOC_RE = re.compile(r"^/api/v2/repos/(?P<repo>[^/]+/[^/]+)/toc$")


class FakeYuque:
    """一个最小的语雀副本：文档 + 目录，全部在内存里（可 dump 到磁盘）。"""

    def __init__(self, repo: str = DEFAULT_REPO, *, seed: bool = False) -> None:
        self.repo = repo
        self.lock = threading.RLock()
        self.docs: dict[int, dict[str, Any]] = {}
        self.toc: list[dict[str, Any]] = []
        self._next_id = 1000
        self._clock = 0
        self.writes: dict[str, int] = {}  # 写操作计数（证明 agent 只读）
        if seed:
            self.seed()

    # -- 时间戳：保证每次写入都严格递增（不然 Windows 上可能出现相同 updated_at）----
    def note_write(self, what: str) -> None:
        with self.lock:
            self.writes[what] = self.writes.get(what, 0) + 1

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-09-19T20:{self._clock // 60:02d}:{self._clock % 60:02d}+08:00"

    # -- 社员视角的写操作 -------------------------------------------------
    def create_doc(
        self,
        *,
        title: str,
        body: str,
        parent_uuid: str = "",
        creator: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self._next_id += 1
            doc_id = self._next_id
            doc = {
                "id": doc_id,
                "slug": uuid.uuid4().hex[:12],
                "title": title,
                "type": "Doc",
                "format": "lake",
                "book_id": 1,
                "body": body,
                "word_count": len(body),
                "created_at": self._stamp(),
                "updated_at": self._stamp(),
                "creator": creator or {"id": 900, "login": "member", "name": "张三"},
            }
            self.docs[doc_id] = doc
            self.toc.append(
                {
                    "uuid": uuid.uuid4().hex,
                    "type": "DOC",
                    "title": title,
                    "url": f"{DOCS_URL}/{doc['slug']}",
                    "slug": doc["slug"],
                    "doc_id": doc_id,
                    "level": 0,
                    "parent_uuid": parent_uuid,
                    "child_uuid": "",
                }
            )
            return doc

    def update_doc(self, doc_id: int, *, title: str | None = None, body: str | None = None) -> dict:
        with self.lock:
            doc = self.docs[doc_id]
            if title is not None:
                doc["title"] = title
                for item in self.toc:
                    if item.get("doc_id") == doc_id:
                        item["title"] = title
            if body is not None:
                doc["body"] = body
            doc["updated_at"] = self._stamp()
            return doc

    def delete_doc(self, doc_id: int) -> dict:
        with self.lock:
            doc = self.docs.pop(doc_id)
            self.toc = [i for i in self.toc if i.get("doc_id") != doc_id]
            return doc

    def create_group(self, title: str, parent_uuid: str = "") -> dict:
        with self.lock:
            item = {
                "uuid": uuid.uuid4().hex,
                "type": "TITLE",
                "title": title,
                "url": "",
                "slug": "",
                "doc_id": "",
                "level": 0,
                "parent_uuid": parent_uuid,
                "child_uuid": "",
            }
            self.toc.append(item)
            return item

    def move_node(self, node_uuid: str, target_uuid: str) -> None:
        with self.lock:
            for item in self.toc:
                if item["uuid"] == node_uuid:
                    item["parent_uuid"] = target_uuid

    # -- 造一个「像真的」知识库 -------------------------------------------
    def seed(self) -> None:
        guide = self.create_doc(
            title="指导文档（必读）",
            body="# 教室借用申请 · 填表说明（必读）\n\n本知识库由 agent 自动维护。\n",
        )
        self.create_group("归档区")
        self.create_group("0914-0920")
        self.create_group("0921-0927")
        # 指导文档按「根目录 + 标题」就能被 agent 自动识别
        for item in self.toc:
            if item.get("doc_id") == guide["id"]:
                item["parent_uuid"] = ""

    # -- 快照（给 driver 读）---------------------------------------------
    def dump(self, path: Path) -> None:
        with self.lock:
            path.write_text(
                json.dumps({"docs": self.docs, "toc": self.toc}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def doc_list(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.lock:
            ordered = [self.docs[k] for k in sorted(self.docs)]
        page = ordered[offset : offset + limit]
        return [{k: v for k, v in d.items() if k != "body"} for d in page]

    def find(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            if key.isdigit() and int(key) in self.docs:
                return self.docs[int(key)]
            for doc in self.docs.values():
                if doc["slug"] == key:
                    return doc
        return None


DOCS_URL = "https://nova.yuque.com/lqogh0/jsjysq"


def make_handler(store: FakeYuque, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "mock-yuque/1.0"
        timeout = 10

        def _send(self, code: int, payload: Any) -> None:
            raw = json.dumps({"data": payload}, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("x-oauth-scopes", SCOPES)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _fail(self, code: int, message: str) -> None:
            raw = json.dumps({"status": code, "message": message}, ensure_ascii=False).encode(
                "utf-8"
            )
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            if token and self.headers.get("X-Auth-Token") != token:
                self._fail(401, "Unauthorized")
                return False
            return True

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        # -- 路由 ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._authed():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/v2/hello":
                self._send(200, {"message": "hello"})
                return
            if parsed.path == "/__stats":  # 测试专用：写操作计数 + 规模
                with store.lock:
                    self._send(
                        200,
                        {
                            "docs": len(store.docs),
                            "toc": len(store.toc),
                            "writes": dict(store.writes),
                            "write_total": sum(store.writes.values()),
                        },
                    )
                return
            if parsed.path == "/api/v2/user":
                self._send(200, {"type": "Group", "login": "lqogh0", "name": "NOVA"})
                return
            if parsed.path == "/api/v2/repos/lqogh0/jsjysq":
                self._send(
                    200, {"id": 1, "slug": "jsjysq", "name": "教室借用申请", "namespace": "lqogh0"}
                )
                return
            if TOC_RE.match(parsed.path):
                with store.lock:
                    self._send(200, list(store.toc))
                return
            if m := DOCS_RE.match(parsed.path):
                key = m.group("key")
                if key:
                    doc = store.find(key)
                    if doc is None:
                        self._fail(404, "文档不存在")
                        return
                    self._send(200, dict(doc))
                    return
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["100"])[0])
                offset = int((query.get("offset") or ["0"])[0])
                self._send(200, store.doc_list(limit, offset))
                return
            self._send(200, {})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return
            parsed = urlparse(self.path)
            if DOCS_RE.match(parsed.path):
                store.note_write("POST /docs")
                payload = self._body()
                doc = store.create_doc(
                    title=str(payload.get("title") or ""),
                    body=str(payload.get("body") or ""),
                )
                doc["updated_at"] = store._stamp()
                self._send(200, dict(doc))
                return
            self._send(200, {})

        def do_PUT(self) -> None:  # noqa: N802
            if not self._authed():
                return
            parsed = urlparse(self.path)
            if TOC_RE.match(parsed.path):
                store.note_write("PUT /toc")
                payload = self._body()
                action = payload.get("action")
                if action == "appendNode" and payload.get("title") and not payload.get("node_uuid"):
                    self._send(
                        200,
                        [store.create_group(str(payload["title"]), payload.get("target_uuid", ""))],
                    )
                    return
                if action == "appendNode" and payload.get("node_uuid"):
                    store.move_node(str(payload["node_uuid"]), str(payload.get("target_uuid", "")))
                    self._send(200, [])
                    return
                for doc_id in payload.get("doc_ids") or []:
                    with store.lock:
                        for item in store.toc:
                            if item.get("doc_id") == doc_id:
                                item["parent_uuid"] = payload.get("target_uuid", "")
                self._send(200, [])
                return
            if m := DOCS_RE.match(parsed.path):
                store.note_write("PUT /docs")
                key = m.group("key") or ""
                doc = store.find(key)
                if doc is None:
                    self._fail(404, "文档不存在")
                    return
                payload = self._body()
                updated = store.update_doc(
                    int(doc["id"]),
                    title=payload.get("title"),
                    body=payload.get("body"),
                )
                self._send(200, dict(updated))
                return
            self._send(200, {})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authed():
                return
            parsed = urlparse(self.path)
            if m := DOCS_RE.match(parsed.path):
                store.note_write("DELETE /docs")
                doc = store.find(m.group("key") or "")
                if doc is None:
                    self._fail(404, "文档不存在")
                    return
                self._send(200, store.delete_doc(int(doc["id"])))
                return
            self._send(200, {})

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if LOG_VERBOSE:
                sys.stderr.write("[mock] " + (fmt % args) + "\n")

    return Handler


LOG_VERBOSE = False


def main() -> int:
    global LOG_VERBOSE
    parser = argparse.ArgumentParser(description="本地仿真语雀 API（只给测试用）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--token", default="local-dev-token")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--seed", action="store_true", help="预置一个像样的知识库")
    parser.add_argument("--state", type=Path, help="把内存状态 dump 到这个文件（便于排查）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    LOG_VERBOSE = args.verbose

    store = FakeYuque(args.repo, seed=args.seed)
    if args.state:
        store.dump(args.state)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(store, args.token))
    httpd.daemon_threads = True
    print(f"仿真语雀已启动：http://{args.host}:{args.port}  token={args.token}  repo={args.repo}")
    print(f"  docs={len(store.docs)} toc={len(store.toc)}  seed={args.seed}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if args.state:
            store.dump(args.state)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
