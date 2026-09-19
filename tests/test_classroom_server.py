"""webhook 报文解析 + HTTP 接收 + 常驻循环单测（不联网）。"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from classroom_fakes import build_harness, make_doc  # type: ignore[import-not-found]

from nju_yuque.classroom import server as server_mod
from nju_yuque.classroom.notify import KIND_DELETED_SUBMITTED
from nju_yuque.classroom.server import (
    ACTION_DELETE,
    ACTION_IGNORE,
    ACTION_UNKNOWN,
    ACTION_UPSERT,
    Runner,
    WebhookEvent,
    make_handler,
    make_server,
    parse_webhook,
)


# ---------------------------------------------------------------- 报文解析
def test_parse_publish_payload() -> None:
    payload = {
        "action_type": "publish",
        "data": {
            "id": 123,
            "title": "新生见面会",
            "slug": "abcdef",
            "book": {"namespace": "lqogh0/jsjysq"},
            "body": "申请人：张三",
        },
    }
    event = parse_webhook(payload)
    assert event.action == ACTION_UPSERT
    assert event.doc_id == 123
    assert event.title == "新生见面会"
    assert event.repo == "lqogh0/jsjysq"
    assert event.usable is True


def test_parse_flat_payload_and_delete() -> None:
    event = parse_webhook({"action": "delete", "doc_id": "77", "title": "读书会"})
    assert event.action == ACTION_DELETE and event.doc_id == 77 and event.usable is True


def test_parse_nested_target_and_repo_slug_variants() -> None:
    event = parse_webhook(
        {
            "action_type": "update",
            "data": {"target": {"id": 5, "slug": "s5"}, "book": {"slug": "jsjysq"}},
        }
    )
    assert event.action == ACTION_UPSERT and event.doc_id == 5 and event.repo == "jsjysq"


def test_parse_ignores_comments_and_unknown_events() -> None:
    assert (
        parse_webhook({"action_type": "comment_create", "data": {"id": 1}}).action == ACTION_IGNORE
    )
    assert parse_webhook({"data": {"id": 1}}).action == ACTION_UNKNOWN
    assert parse_webhook({}).usable is False


def test_parse_tolerates_garbage() -> None:
    assert parse_webhook({"data": "not a dict", "action_type": "publish"}).doc_id == 0
    assert parse_webhook({"action_type": "publish", "doc_id": "abc"}).doc_id == 0


# ---------------------------------------------------------------- HTTP
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(url: str, payload: object, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - 本地回环地址，测试用
        url, data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_server_accepts_events(tmp_path: Path) -> None:
    got: list[WebhookEvent] = []
    port = _free_port()
    httpd = make_server(
        host="127.0.0.1",
        port=port,
        handler=make_handler(got.append, path="/yuque/webhook", dump_dir=tmp_path / "raw"),
    )
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post(
            f"http://127.0.0.1:{port}/yuque/webhook",
            {"action_type": "publish", "data": {"id": 9, "title": "读书会"}},
        )
        assert status == 200 and body["doc_id"] == 9 and body["action"] == ACTION_UPSERT
        assert len(got) == 1

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:  # noqa: S310
            assert resp.status == 200 and json.loads(resp.read())["ok"] is True

        code, _ = _post(f"http://127.0.0.1:{port}/nope", {"action_type": "publish"})
        assert code == 404

        # 原始报文落盘，方便对接时核对字段
        assert list((tmp_path / "raw").glob("webhook-*.json"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_server_enforces_secret() -> None:
    got: list[WebhookEvent] = []
    port = _free_port()
    httpd = make_server(
        host="127.0.0.1",
        port=port,
        handler=make_handler(got.append, secret="s3cret"),
    )
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/yuque/webhook"
        assert _post(url, {"action_type": "publish", "data": {"id": 1}})[0] == 401
        assert got == []
        assert _post(url + "?token=s3cret", {"action_type": "publish", "data": {"id": 1}})[0] == 200
        assert (
            _post(url, {"action_type": "publish", "data": {"id": 2}}, {"X-Yuque-Token": "s3cret"})[
                0
            ]
            == 200
        )
        assert (
            _post(
                url,
                {"action_type": "publish", "data": {"id": 3}},
                {"Authorization": "Bearer s3cret"},
            )[0]
            == 200
        )
        assert len(got) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- Runner
def _runner(tmp_path: Path, **kw: object) -> tuple[Runner, object]:
    h = build_harness(tmp_path, [make_doc(1)])
    runner = Runner(h.pipeline, h.store, **kw)  # type: ignore[arg-type]
    return runner, h


def test_runner_tick_logs_and_processes(tmp_path: Path) -> None:
    logs: list[str] = []
    runner, h = _runner(tmp_path, interval=5, log=logs.append)
    report = runner.tick()
    assert report.accepted == ["2026-09-16-7_8-1"]
    assert any("已受理" in line for line in logs)


def test_runner_drain_collects_events(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, interval=0.01)
    assert runner._drain(timeout=0.01) == []
    runner.events.put(WebhookEvent(action=ACTION_UPSERT, doc_id=1))
    runner.events.put(WebhookEvent(action=ACTION_UPSERT, doc_id=2))
    assert [e.doc_id for e in runner._drain(timeout=0.01)] == [1, 2]


def test_runner_handle_events_delete(tmp_path: Path) -> None:
    logs: list[str] = []
    runner, h = _runner(tmp_path, interval=1, log=logs.append)
    runner.tick()  # 先受理，让 store 有记录
    h.box.ack_all()
    h.source.remove(1)  # 真删了（删除事件也要能读出 404 才认）
    assert runner.handle_events([WebhookEvent(action=ACTION_DELETE, doc_id=1)]) == set()
    assert len(h.notices(KIND_DELETED_SUBMITTED)) == 1
    assert any("已删除" in line for line in logs)


def test_runner_handle_events_upsert_asks_for_a_reread(tmp_path: Path) -> None:
    """更新事件只要求「定向重读这一篇」，真正的处理交给紧接着的那一轮。"""
    runner, h = _runner(tmp_path, interval=1, log=lambda _x: None)
    force = runner.handle_events([WebhookEvent(action=ACTION_UPSERT, doc_id=1)])
    assert force == {1}
    assert h.notices() == []  # 还没处理
    runner.tick(force=force)
    assert h.notices()


def test_runner_without_port_has_no_server(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, interval=1)
    assert runner.serve_background() is None
    runner.stop()


def _wait(pred: object, timeout: float = 10.0) -> bool:
    """等某个条件成立（轮询式等待，避免测试变 flaky）。

    条件里的异常一律当作「还没好」——典型场景是服务还在启动、连接被拒
    （Linux 上是 ConnectionRefused，Windows 上是另一种错，别让平台差异决定成败）。
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if callable(pred) and pred():
                return True
        except Exception:  # noqa: BLE001 - 探活阶段的任何异常都算「还没起来」
            pass
        time.sleep(0.05)
    return False


def test_run_forever_polls_and_stops_cleanly(tmp_path: Path) -> None:
    import threading

    logs: list[str] = []
    runner, h = _runner(tmp_path, interval=0.05, log=logs.append)
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    assert _wait(lambda: bool(h.notices()), timeout=10), logs
    runner.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert any("开始常驻" in line for line in logs)
    assert any("已受理" in line for line in logs)


def test_run_forever_handles_webhook_end_to_end(tmp_path: Path) -> None:
    """真起一个 HTTP 服务：POST 一条发布事件 → 立刻受理；再 POST 删除事件 → 立刻通知。"""
    import threading

    logs: list[str] = []
    port = _free_port()
    runner, h = _runner(tmp_path, interval=30, host="127.0.0.1", port=port, log=logs.append)
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/yuque/webhook"
        # 先把服务等起来（run_forever 里才 bind，早发会 ConnectionRefused）
        assert _wait(
            lambda: _post(url, {"action_type": "publish", "data": {"id": 1}})[0] == 200, timeout=15
        )
        assert _wait(lambda: bool(h.notices("accepted")), timeout=10), logs
        # 别的知识库发来的删除事件必须被忽略
        assert (
            _post(
                url,
                {
                    "action_type": "delete",
                    "data": {"id": 1, "book": {"namespace": "other/book"}},
                },
            )[0]
            == 200
        )
        assert _wait(lambda: any("别的知识库" in line for line in logs), timeout=10), logs
        assert not h.notices("deleted_submitted")
        # 自己的删除事件才处理（而且要先真的读不到）
        assert _post(url, {"action_type": "delete", "data": {"id": 1}})[0] == 200
        assert _wait(lambda: any("仍读得到" in line for line in logs), timeout=10), logs
        assert not h.notices("deleted_submitted")  # 文档还在 → 不发「不允许撤回」
        h.source.remove(1)
        assert _post(url, {"action_type": "delete", "data": {"id": 1}})[0] == 200
        assert _wait(lambda: bool(h.notices("deleted_submitted")), timeout=10), logs
    finally:
        runner.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_run_forever_survives_a_failing_round(tmp_path: Path) -> None:
    import threading

    logs: list[str] = []
    runner, h = _runner(tmp_path, interval=0.05, log=logs.append)
    h.source.fail = True  # 每轮都报错：常驻循环不能被打死
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    assert _wait(lambda: any("读取知识库失败" in line for line in logs), timeout=10), logs
    assert thread.is_alive()
    h.source.fail = False
    runner.stop()
    thread.join(timeout=10)


def test_webhook_path_default_is_documented() -> None:
    assert server_mod.WEBHOOK_PATH == "/yuque/webhook"


# ---------------------------------------------------------------- 安全 / 健壮性
def test_dump_filename_ignores_payload_fields(tmp_path: Path) -> None:
    """报文里的字段是外部输入，不能拿来拼文件名（否则可路径穿越写任意文件）。"""
    dump = tmp_path / "raw"
    got: list[WebhookEvent] = []
    port = _free_port()
    httpd = make_server(
        host="127.0.0.1", port=port, handler=make_handler(got.append, dump_dir=dump)
    )
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = _post(
            f"http://127.0.0.1:{port}/yuque/webhook",
            {"_ts": "../../pwn", "action_type": "publish", "data": {"id": 1}},
        )
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()

    files = list(dump.glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == dump
    assert not (tmp_path / "pwn.json").exists()
    assert not (tmp_path.parent / "pwn.json").exists()


def test_handler_rejects_oversized_body(tmp_path: Path) -> None:
    """Content-Length 超大时直接 413，不能按声明的长度去分配内存。"""
    import http.client

    got: list[WebhookEvent] = []
    port = _free_port()
    httpd = make_server(host="127.0.0.1", port=port, handler=make_handler(got.append))
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/yuque/webhook")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(4 << 20))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 413
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert got == []


def test_remove_action_is_not_treated_as_delete() -> None:
    """「从目录移除 / 取消发布」不是删除，交给轮询确认，免得误报「不允许撤回」。"""
    assert parse_webhook({"action_type": "remove", "data": {"id": 1}}).action == ACTION_UNKNOWN
    # 取消发布 → 直接忽略（既不是删除也不是更新）
    assert parse_webhook({"action_type": "unpublish", "data": {"id": 1}}).action == ACTION_IGNORE
    assert parse_webhook({"action_type": "doc_delete", "data": {"id": 1}}).action == ACTION_DELETE
    assert parse_webhook({"action_type": "trash", "data": {"id": 1}}).action == ACTION_DELETE


def test_events_from_other_repos_are_ignored(tmp_path: Path) -> None:
    logs: list[str] = []
    runner, h = _runner(tmp_path, interval=1, log=logs.append)
    assert runner.belongs_here(WebhookEvent(repo="")) is True
    assert runner.belongs_here(WebhookEvent(repo="lqogh0/jsjysq")) is True
    assert runner.belongs_here(WebhookEvent(repo="jsjysq")) is True
    assert runner.belongs_here(WebhookEvent(repo="other/book")) is False

    # 别的库发来的删除事件不能动本地记录
    runner.tick()
    h.box.ack_all()
    forced = runner.handle_events([WebhookEvent(action=ACTION_DELETE, doc_id=1, repo="other/book")])
    assert forced == set() and h.notices() == []
    assert h.state_file()["docs"]["1"]["status"] == "submitted"
    assert any("别的知识库" in line for line in logs)


class _BoomNotifier:
    """写盘永远失败（模拟磁盘满 / 权限不足）。"""

    def send(self, notice: object) -> None:
        raise OSError("disk full")


def test_run_forever_survives_notifier_failures(tmp_path: Path) -> None:
    """notifier 写盘失败不能打死常驻进程（整轮处理与 webhook 事件两条路径都要活）。"""
    import threading

    logs: list[str] = []
    h = build_harness(tmp_path, [make_doc(1)])
    runner = Runner(h.pipeline, h.store, interval=0.05, log=logs.append)
    runner.pipeline.notifier = _BoomNotifier()  # type: ignore[assignment]
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    assert _wait(lambda: any("本轮处理异常" in line for line in logs), timeout=10), logs

    # webhook 删除事件走 handle_delete → notifier 也炸：同样不能打死循环
    before = len([x for x in logs if "本轮处理异常" in x])
    runner.handle_events([WebhookEvent(action=ACTION_DELETE, doc_id=1)]) if False else None
    try:
        runner.handle_events([WebhookEvent(action=ACTION_DELETE, doc_id=1)])
    except OSError:
        pass  # handle_events 本身可以抛，但 run_forever 一定兜住
    assert thread.is_alive()
    runner.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len([x for x in logs if "本轮处理异常" in x]) >= before


def test_runner_poll_interval_has_a_floor(tmp_path: Path) -> None:
    runner, _ = _runner(tmp_path, interval=0)
    assert runner.interval == 1.0


def test_run_forever_survives_delete_event_failure(tmp_path: Path) -> None:
    """webhook 删除事件处理中抛异常（磁盘满）→ run_forever 必须活着继续轮询。"""
    import threading

    logs: list[str] = []
    h = build_harness(tmp_path, [make_doc(1)])
    runner = Runner(h.pipeline, h.store, interval=0.05, log=logs.append)

    def boom(_doc_id: int) -> None:
        raise OSError("disk full")

    runner.pipeline.handle_delete = boom  # type: ignore[method-assign]
    thread = threading.Thread(target=runner.run_forever, daemon=True)
    thread.start()
    try:
        runner.events.put(WebhookEvent(action=ACTION_DELETE, doc_id=1))
        assert _wait(lambda: any("本轮处理异常" in line for line in logs), timeout=10), logs
        assert thread.is_alive()
    finally:
        runner.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
