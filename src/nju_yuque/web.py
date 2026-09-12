"""语雀网页内部接口客户端（``/api/*``，Cookie 模式）。

这是「路线 B」：官方 OpenAPI 覆盖不到的「评论 / @人 / 写文档」只能走这里。
它属于**非公开接口**，语雀改版可能失效，因此设置成「登录模式下才可用」，
并且所有失败都给出明确提示而不是静默。

写操作四要素（社区实测，缺一不可）：

1. Cookie 带 ``_yuque_session``（登录态）与 ``yuque_ctoken``（CSRF）；
2. 请求头 ``X-CSRF-Token: <yuque_ctoken>``；
3. 请求头 ``X-Requested-With: XMLHttpRequest``，body 必须是 JSON；
4. 带正确的 ``Referer``。
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import quote

import httpx

from .config import USER_AGENT
from .errors import WrongModeError, YuqueError, raise_for_status
from .session import SESSION_COOKIE, Credentials

PAGE_SIZE = 100


class YuqueWeb:
    """Cookie 模式客户端。"""

    def __init__(self, cred: Credentials) -> None:
        if not cred.is_cookie:
            raise WrongModeError(
                "该操作需要 Cookie 模式（网页登录态）：请先运行 `yuque login`（会开浏览器登录）"
            )
        if SESSION_COOKIE not in cred.cookies:
            raise WrongModeError("Cookie 里缺少 _yuque_session，登录态不完整；请重新 `yuque login`")
        self.host = cred.host.rstrip("/")
        self.cred = cred
        self.client = httpx.Client(
            base_url=self.host,
            headers={"User-Agent": USER_AGENT},
            cookies=dict(cred.cookies),
            timeout=30,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> YuqueWeb:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层请求 ---------------------------------------------------------
    def _headers(self, *, write: bool, referer: str | None) -> dict[str, str]:
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": referer or f"{self.host}/",
        }
        if write:
            token = self.cred.csrf_token
            if not token:
                raise WrongModeError("写操作缺少 CSRF token（yuque_ctoken）；请重新 `yuque login`")
            headers["Content-Type"] = "application/json"
            headers["X-CSRF-Token"] = token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
        referer: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """发一次请求，把网络层异常统一包成 YuqueError。"""
        try:
            resp = self.client.request(
                method,
                path,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json_body,
                headers=self._headers(write=write, referer=referer),
            )
        except httpx.HTTPError as exc:
            raise YuqueError(f"网络请求失败（{type(exc).__name__}）：{exc}") from exc
        if resp.status_code >= 400:
            message = ""
            try:
                payload = resp.json()
                message = str(payload.get("message") or payload.get("msg") or "")
            except ValueError:
                message = resp.text[:300]
            raise_for_status(resp.status_code, message)
        try:
            payload = resp.json()
        except ValueError:
            return resp.text
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    # -- 身份 -------------------------------------------------------------
    def mine(self) -> dict[str, Any]:
        data = self._request("GET", "/api/mine")
        return data if isinstance(data, dict) else {}

    def books(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/mine/books") or []
        return data if isinstance(data, list) else []

    def group_id(self, group: str = "") -> int | None:
        """从团队快捷入口里找 group_id（@人 / 评论需要）。"""
        data = self._request("GET", "/api/mine/group_quick_links") or []
        want = (group or self.cred.group or "").lower()
        for item in data:
            login = str(item.get("login") or item.get("slug") or "").lower()
            if login == want or not want:
                return item.get("group_id") or item.get("id")
        return None

    # -- 文档 -------------------------------------------------------------
    def docs(self, book_id: int, *, limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = (
                self._request("GET", "/api/docs", params={"book_id": book_id, "offset": offset})
                or []
            )
            out.extend(page)
            if len(page) < PAGE_SIZE or (limit is not None and len(out) >= limit):
                break
            offset += len(page)
        return out[:limit] if limit is not None else out

    def doc_detail(
        self,
        book_id: int,
        slug_or_id: str | int,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/api/docs/{slug_or_id}",
            params={"book_id": book_id, "mode": mode, "include_contributors": "true"},
        )
        return data if isinstance(data, dict) else {}

    def doc_markdown(self, space: str, repo: str, slug: str, *, linebreak: bool = False) -> str:
        """直接取渲染前 Markdown（``/{space}/{repo}/{slug}/markdown``）。"""
        text = self._request(
            "GET",
            f"/{space}/{repo}/{slug}/markdown",
            params={"plain": "true", "linebreak": str(linebreak).lower(), "anchor": "false"},
            referer=f"{self.host}/{space}/{repo}/{slug}",
        )
        return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)

    # -- 评论 -------------------------------------------------------------
    def comments(self, doc_id: int) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/comments",
            params={"commentable_id": doc_id, "commentable_type": "Doc"},
        )
        return data if isinstance(data, list) else []

    def search_users(
        self, *, target_type: str, target_id: int, group_id: int, q: str
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/api/users/complete",
            params={
                "target_type": target_type,
                "target_id": target_id,
                "group_id": group_id,
                "q": q,
            },
        )
        return data if isinstance(data, list) else []

    def create_comment(
        self,
        *,
        doc_id: int,
        body: str,
        referer: str | None = None,
        mention: list[str] | None = None,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        """发评论；``mention`` 传语雀 login（或姓名），会转成 @ 卡片触发通知。"""
        payload: dict[str, Any] = {
            "commentable_id": doc_id,
            "commentable_type": "Doc",
            "body": body,
        }
        if mention:
            resolved = []
            for who in mention:
                user = self._resolve_user(who, doc_id=doc_id, group_id=group_id)
                resolved.append(
                    (
                        str(user.get("login") or who),
                        str(user.get("name") or who),
                        int(user.get("id") or 0),
                    )
                )
            body_asl, body_html = _comment_with_mentions(body, resolved, self.host)
            payload["mention"] = json.dumps([r[0] for r in resolved], ensure_ascii=False)
            payload["format"] = "lake"
            payload["body_asl"] = body_asl
            payload["body"] = body_html

        data = self._request(
            "POST",
            "/api/comments",
            json_body=payload,
            write=True,
            referer=referer,
        )
        return data if isinstance(data, dict) else {"raw": data}

    def delete_comment(self, comment_id: int) -> Any:
        return self._request("DELETE", f"/api/comments/{comment_id}", write=True)

    def _resolve_user(self, who: str, *, doc_id: int, group_id: int | None) -> dict[str, Any]:
        if not group_id:
            return {}
        users = self.search_users(target_type="Doc", target_id=doc_id, group_id=group_id, q=who)
        return users[0] if users else {}


# ---------------------------------------------------------------- @ 卡片构造


def _random_id() -> str:
    return "u" + secrets.token_hex(4)


def _mention_card(
    login: str, name: str, userid: int, span_id: str, base_url: str
) -> tuple[str, str]:
    value = json.dumps(
        {"login": login, "name": name, "userid": userid, "workId": "", "id": span_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    card = (
        f'<card type="inline" name="mention" '
        f'data-login="{login}" data-name="{name}" data-userid="{userid}" '
        f'value="data:{quote(value, safe="")}"></card>'
    )
    span = (
        f'<span id="{span_id}" class="ne-mention"><a href="{base_url}/{login}">@{name}</a></span>'
    )
    return card, span


def _comment_with_mentions(
    text: str, mentions: list[tuple[str, str, int]], base_url: str
) -> tuple[str, str]:
    """构造带 @ 的 Lake 评论，返回 ``(body_asl, body_html)``。"""
    pid = _random_id()
    cards, spans = "", ""
    for login, name, userid in mentions:
        card, span = _mention_card(login, name, userid, _random_id(), base_url)
        cards += card
        spans += span

    body_asl = (
        "<!doctype lake>"
        '<meta name="doc-version" content="1" />'
        '<meta name="viewport" content="adapt" />'
        f'<p data-lake-id="{pid}" id="{pid}">{cards}'
        f'<span data-lake-id="{_random_id()}" id="{_random_id()}"> </span></p>'
    )
    body_html = f'<div class="lake-content" typography="traditional"><p id="{pid}" class="ne-p">{spans}<span class="ne-text"> </span></p>'
    if text:
        body_asl += f'<p data-lake-id="{_random_id()}" id="{_random_id()}"><span>{text}</span></p>'
        body_html += f'<p class="ne-p"><span class="ne-text">{text}</span></p>'
    body_html += "</div>"
    return body_asl, body_html


def web_client(cred: Credentials) -> YuqueWeb:
    """工厂函数。"""
    return YuqueWeb(cred)
