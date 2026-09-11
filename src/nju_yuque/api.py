"""语雀官方 OpenAPI 客户端（``/api/v2/*``，``X-Auth-Token``）。

这是「路线 A」：零浏览器、最省事。能不能写取决于令牌 scope；
只读令牌能覆盖「列知识库 / 列文档 / 搜文档 / 读正文 / 读表格」。

所有请求都是 GET；本客户端刻意不实现任何写操作，避免误伤。
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import DEFAULT_GROUP, USER_AGENT
from .errors import YuqueError, raise_for_status
from .models import Doc, Member, Repo, TocItem

PAGE_SIZE = 100  # 语雀文档列表 limit 上限就是 100，超过会 422


class YuqueApi:
    """官方 OpenAPI 客户端。"""

    def __init__(self, host: str, token: str, *, group: str = "") -> None:
        self.host = host.rstrip("/")
        self.group = group or DEFAULT_GROUP
        self.token = token
        self.scopes = ""
        self.client = httpx.Client(
            base_url=self.host,
            headers={"X-Auth-Token": token, "User-Agent": USER_AGENT},
            timeout=30,
            follow_redirects=True,
        )

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> YuqueApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------
    def _get_payload(self, path: str, **params: Any) -> Any:
        """发一次 GET，返回语雀原始响应体（不拆 ``data``）。"""
        resp = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        scopes = resp.headers.get("x-oauth-scopes")
        if scopes:
            self.scopes = scopes
        if resp.status_code >= 400:
            message = ""
            try:
                payload = resp.json()
                message = str(payload.get("message") or payload.get("error") or "")
            except ValueError:
                message = resp.text[:300]
            raise_for_status(resp.status_code, message)
        return resp.json()

    def _get(self, path: str, **params: Any) -> Any:
        payload = self._get_payload(path, **params)
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    # -- 身份 / 自检 ------------------------------------------------------
    def hello(self) -> str:
        data = self._get("/api/v2/hello")
        return str(data.get("message", "")) if isinstance(data, dict) else str(data)

    def whoami(self) -> dict[str, Any]:
        data = self._get("/api/v2/user")
        return data if isinstance(data, dict) else {}

    # -- 知识库 -----------------------------------------------------------
    def repos(self, group: str | None = None) -> list[Repo]:
        login = group or self.group
        data = self._get(f"/api/v2/groups/{login}/repos", limit=PAGE_SIZE)
        return [Repo.model_validate(x) for x in data or []]

    def repo(self, repo: str) -> Repo:
        data = self._get(f"/api/v2/repos/{repo}")
        return Repo.model_validate(data)

    def toc(self, repo: str) -> list[TocItem]:
        data = self._get(f"/api/v2/repos/{repo}/toc") or []
        items = [TocItem.model_validate(x) for x in data]
        by_uuid = {i.uuid: i for i in items}
        for item in items:
            depth, parent = 0, item.parent_uuid
            while parent and parent in by_uuid:
                depth += 1
                parent = by_uuid[parent].parent_uuid
            item.depth = depth
        return items

    # -- 文档 -------------------------------------------------------------
    def docs(self, repo: str, *, limit: int | None = None, offset: int = 0) -> list[Doc]:
        out: list[Doc] = []
        while True:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(out))
            if size <= 0:
                break
            page = self._get(f"/api/v2/repos/{repo}/docs", limit=size, offset=offset)
            page = page or []
            out.extend(Doc.model_validate(x) for x in page)
            if len(page) < size:
                break
            offset += len(page)
        return out

    def doc(self, repo: str, slug: str, *, raw: bool = True) -> Doc:
        data = self._get(f"/api/v2/repos/{repo}/docs/{slug}", raw=1 if raw else None)
        return Doc.model_validate(data)

    def doc_versions(self, doc_id: int) -> list[dict[str, Any]]:
        data = self._get("/api/v2/doc_versions", doc_id=doc_id)
        return data if isinstance(data, list) else []

    def search(
        self,
        q: str,
        *,
        type: str = "doc",  # noqa: A002 - 对齐语雀参数名
        scope: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        data = self._get_payload("/api/v2/search", q=q, type=type, scope=scope, page=page)
        return data if isinstance(data, dict) else {"data": data}

    # -- 团队 -------------------------------------------------------------
    def members(self, group: str | None = None) -> list[Member]:
        login = group or self.group
        out: list[Member] = []
        offset = 0
        while True:  # PageSize 固定 100，只能用 offset 翻页
            page = self._get(f"/api/v2/groups/{login}/users", offset=offset) or []
            for raw in page:
                user = raw.get("user") or {}
                out.append(
                    Member(
                        user_id=int(user.get("id") or 0),
                        name=str(user.get("name") or ""),
                        login=str(user.get("login") or ""),
                        role=raw.get("role"),
                    )
                )
            if len(page) < PAGE_SIZE:
                break
            offset += len(page)
        return out

    def statistics(self, group: str | None = None) -> dict[str, Any]:
        login = group or self.group
        data = self._get(f"/api/v2/groups/{login}/statistics")
        return data if isinstance(data, dict) else {}


def open_api(host: str, token: str, *, group: str = "") -> YuqueApi:
    """工厂函数，语义更清晰。"""
    if not token:
        raise YuqueError("缺少令牌：请传 --token，或先运行 `yuque login --token <令牌>`")
    return YuqueApi(host, token, group=group)
