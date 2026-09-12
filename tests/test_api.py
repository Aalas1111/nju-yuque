"""官方 OpenAPI 客户端测试：用 httpx MockTransport，不触网。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nju_yuque.api import YuqueApi
from nju_yuque.errors import (
    AuthExpiredError,
    InsufficientScopeError,
    NotFoundError,
    RateLimitedError,
)


class Recorder:
    """记录请求并返回固定响应的假传输层。"""

    def __init__(
        self, status: int = 200, payload: Any = None, headers: dict[str, str] | None = None
    ):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.payload = payload if payload is not None else {"data": {}}
        self.headers = headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload, headers=self.headers)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_api(recorder: Recorder) -> YuqueApi:
    api = YuqueApi("https://nova.yuque.com", "t0k", group="ghxd00")
    api.client.close()
    api.client = httpx.Client(
        base_url="https://nova.yuque.com",
        transport=httpx.MockTransport(recorder),
        headers={"X-Auth-Token": "t0k"},
    )
    return api


def test_can_write_scope_rules() -> None:
    api = YuqueApi("https://nova.yuque.com", "t0k")
    api.scopes = "group:read,repo:read,doc:read,statistic:read,private_search"
    assert api.can_write("doc") is False
    assert api.can_write("repo") is False

    api.scopes = "group,repo,doc,statistic:read,private_search"
    assert api.can_write("doc") is True
    assert api.can_write("repo") is True
    assert api.can_write("statistic") is False  # statistic:read 仍是只读

    api.scopes = "doc:write"
    assert api.can_write("doc") is True


def test_scopes_updated_from_response_header() -> None:
    rec = Recorder(payload={"data": {"message": "hi"}}, headers={"x-oauth-scopes": "doc,repo"})
    api = make_api(rec)
    api.hello()
    assert api.scopes == "doc,repo"
    assert api.can_write("doc") is True


def test_create_doc_payload() -> None:
    rec = Recorder(payload={"data": {"id": 1, "slug": "s", "title": "T"}})
    api = make_api(rec)
    doc = api.create_doc("ghxd00/mrge27", title="T", body="# hi", slug="s", public=2)
    assert doc.id == 1
    assert rec.last.method == "POST"
    assert str(rec.last.url) == "https://nova.yuque.com/api/v2/repos/ghxd00/mrge27/docs"
    body = rec.last.read().decode()
    assert '"title":"T"' in body.replace(" ", "")
    assert '"format":"markdown"' in body.replace(" ", "")
    assert '"public":2' in body.replace(" ", "")


def test_update_doc_only_sends_given_fields() -> None:
    rec = Recorder(payload={"data": {"id": 7, "slug": "s", "title": "new"}})
    api = make_api(rec)
    api.update_doc("ghxd00/mrge27", 7, title="new")
    assert rec.last.method == "PUT"
    body = rec.last.read().decode().replace(" ", "")
    assert '"title":"new"' in body
    assert '"body"' not in body


def test_delete_doc() -> None:
    rec = Recorder(payload={"data": {"id": 9, "slug": "s", "title": "gone"}})
    api = make_api(rec)
    api.delete_doc("ghxd00/mrge27", 9)
    assert rec.last.method == "DELETE"
    assert str(rec.last.url).endswith("/api/v2/repos/ghxd00/mrge27/docs/9")


def test_toc_add_uses_child_mode() -> None:
    rec = Recorder(payload={"data": []})
    api = make_api(rec)
    api.toc_add("ghxd00/mrge27", doc_ids=[11], target_uuid="PARENT")
    body = rec.last.read().decode().replace(" ", "")
    assert rec.last.method == "PUT"
    assert str(rec.last.url).endswith("/api/v2/repos/ghxd00/mrge27/toc")
    # action_mode 必须是 child（sibling + target_uuid 实测不生效）
    assert '"action":"appendNode"' in body
    assert '"action_mode":"child"' in body
    assert '"doc_ids":[11]' in body
    assert '"target_uuid":"PARENT"' in body


def test_toc_add_title_node() -> None:
    rec = Recorder(payload={"data": []})
    api = make_api(rec)
    api.toc_add("ghxd00/mrge27", title="分组", node_type="TITLE")
    body = rec.last.read().decode().replace(" ", "")
    assert '"type":"TITLE"' in body
    assert '"title":"分组"' in body
    assert "doc_ids" not in body


def test_create_repo_payload() -> None:
    rec = Recorder(payload={"data": {"id": 5, "slug": "sr", "name": "自测库"}})
    api = make_api(rec)
    repo = api.create_repo(name="自测库", slug="sr", description="d", public=2)
    assert repo.id == 5
    assert rec.last.method == "POST"
    assert str(rec.last.url).endswith("/api/v2/groups/ghxd00/repos")
    body = rec.last.read().decode().replace(" ", "")
    assert '"name":"自测库"' in body
    assert '"slug":"sr"' in body


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, AuthExpiredError),
        (403, InsufficientScopeError),
        (404, NotFoundError),
        (429, RateLimitedError),
    ],
)
def test_error_mapping(status: int, exc: type[Exception]) -> None:
    rec = Recorder(status=status, payload={"status": status, "message": "boom"})
    api = make_api(rec)
    with pytest.raises(exc):
        api.hello()


def test_error_message_is_surfaced() -> None:
    rec = Recorder(status=401, payload={"status": 401, "message": "请给此 Token 添加 doc 权限"})
    api = make_api(rec)
    with pytest.raises(AuthExpiredError) as info:
        api.hello()
    assert "请给此 Token 添加 doc 权限" in str(info.value)


def test_toc_move_uses_append_node_with_node_uuid() -> None:
    """回归：移动已有节点必须 appendNode+node_uuid；editNode+target_uuid 会静默失败。"""
    rec = Recorder(payload={"data": []})
    api = make_api(rec)
    api.toc_move("ghxd00/mrge27", node_uuid="KID", target_uuid="PARENT")
    body = rec.last.read().decode().replace(" ", "")
    assert rec.last.method == "PUT"
    assert str(rec.last.url).endswith("/api/v2/repos/ghxd00/mrge27/toc")
    assert '"action":"appendNode"' in body
    assert '"action_mode":"child"' in body
    assert '"node_uuid":"KID"' in body
    assert '"target_uuid":"PARENT"' in body
    assert "doc_ids" not in body
