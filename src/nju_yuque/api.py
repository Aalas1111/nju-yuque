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
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """发一次请求，返回语雀原始响应体（不拆 ``data``）。"""
        resp = self.client.request(
            method,
            path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json_body,
        )
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

    def _get_payload(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    def _get(self, path: str, **params: Any) -> Any:
        return self._unwrap(self._get_payload(path, **params))

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

    # -- 写：能力判断 -----------------------------------------------------
    @property
    def scopes_set(self) -> set[str]:
        return {s.strip() for s in (self.scopes or "").split(",") if s.strip()}

    def can_write(self, kind: str = "doc") -> bool:
        """令牌是否具备某类对象的写权限。"""
        from .session import scope_allows_write

        return scope_allows_write(self.scopes, kind)

    # -- 写：文档 ---------------------------------------------------------
    def create_doc(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        slug: str | None = None,
        public: int | None = None,
        format: str = "markdown",  # noqa: A002 - 对齐语雀字段名
    ) -> Doc:
        payload: dict[str, Any] = {"title": title, "body": body, "format": format}
        if slug:
            payload["slug"] = slug
        if public is not None:
            payload["public"] = public
        data = self._unwrap(self._request("POST", f"/api/v2/repos/{repo}/docs", json_body=payload))
        return Doc.model_validate(data)

    def update_doc(
        self,
        repo: str,
        doc_id: int | str,
        *,
        title: str | None = None,
        body: str | None = None,
        slug: str | None = None,
        public: int | None = None,
        format: str = "markdown",  # noqa: A002
    ) -> Doc:
        payload: dict[str, Any] = {"format": format}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if slug is not None:
            payload["slug"] = slug
        if public is not None:
            payload["public"] = public
        data = self._unwrap(
            self._request("PUT", f"/api/v2/repos/{repo}/docs/{doc_id}", json_body=payload)
        )
        return Doc.model_validate(data)

    def delete_doc(self, repo: str, doc_id: int | str) -> Doc:
        data = self._unwrap(self._request("DELETE", f"/api/v2/repos/{repo}/docs/{doc_id}"))
        return Doc.model_validate(data)

    # -- 写：知识库 -------------------------------------------------------
    def create_repo(
        self,
        *,
        name: str,
        slug: str,
        group: str | None = None,
        description: str | None = None,
        public: int = 2,
        enhanced_privacy: bool = False,
    ) -> Repo:
        login = group or self.group
        payload: dict[str, Any] = {"name": name, "slug": slug, "public": public}
        if description is not None:
            payload["description"] = description
        if enhanced_privacy:
            payload["enhancedPrivacy"] = True
        data = self._unwrap(
            self._request("POST", f"/api/v2/groups/{login}/repos", json_body=payload)
        )
        return Repo.model_validate(data)

    def delete_repo(self, repo: str) -> Repo:
        data = self._unwrap(self._request("DELETE", f"/api/v2/repos/{repo}"))
        return Repo.model_validate(data)

    # -- 写：目录 ---------------------------------------------------------
    def toc_add(
        self,
        repo: str,
        *,
        doc_ids: list[int] | None = None,
        title: str | None = None,
        node_type: str = "DOC",
        target_uuid: str | None = None,
        url: str | None = None,
        prepend: bool = False,
    ) -> list[TocItem]:
        """把新建的文档 / 分组标题 / 外链挂到目录（默认尾巴追加）。

        实测：``action_mode`` 必须用 ``child``（无 ``target_uuid`` 时即挂在根下）；
        ``sibling`` 在带 ``target_uuid`` 时不生效。另注意目录读取有写后延迟（秒级）。
        """
        payload: dict[str, Any] = {
            "action": "prependNode" if prepend else "appendNode",
            "action_mode": "child",
            "type": node_type,
        }
        if doc_ids:
            payload["doc_ids"] = doc_ids
        if title:
            payload["title"] = title
        if url:
            payload["url"] = url
            payload["open_window"] = 0
        if target_uuid:
            payload["target_uuid"] = target_uuid
        data = self._unwrap(self._request("PUT", f"/api/v2/repos/{repo}/toc", json_body=payload))
        return [TocItem.model_validate(x) for x in (data or [])]

    def toc_remove(
        self, repo: str, *, node_uuid: str, with_children: bool = False
    ) -> list[TocItem]:
        payload = {
            "action": "removeNode",
            "action_mode": "child" if with_children else "sibling",
            "node_uuid": node_uuid,
        }
        data = self._unwrap(self._request("PUT", f"/api/v2/repos/{repo}/toc", json_body=payload))
        return [TocItem.model_validate(x) for x in (data or [])]

    def toc_edit(
        self,
        repo: str,
        *,
        node_uuid: str,
        title: str | None = None,
        url: str | None = None,
        open_window: bool | None = None,
    ) -> list[TocItem]:
        payload: dict[str, Any] = {
            "action": "editNode",
            "action_mode": "sibling",
            "node_uuid": node_uuid,
        }
        if title is not None:
            payload["title"] = title
        if url is not None:
            payload["url"] = url
            payload["type"] = "LINK"
        if open_window is not None:
            payload["open_window"] = 1 if open_window else 0
        data = self._unwrap(self._request("PUT", f"/api/v2/repos/{repo}/toc", json_body=payload))
        return [TocItem.model_validate(x) for x in (data or [])]

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
