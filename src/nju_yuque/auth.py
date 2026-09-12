"""登录 / 自检。

三种入口：

- ``login_token()``  —— 官方令牌（推荐，零浏览器，可长期使用）
- ``login_cookie_string()`` —— 直接粘贴网页 Cookie（无 Playwright 时的兜底）
- ``login_browser()`` —— 弹有头浏览器，人工登录后抓取 Cookie（解锁评论/写）

关键实现细节：**未登录的游客也会下发 ``_yuque_session``**，所以不能靠「有没有这个
Cookie」判断登录成功，必须真的请求一次 ``/api/mine``。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .api import YuqueApi
from .browser import launch
from .config import BROWSER_CHOICES, DEFAULT_GROUP, DEFAULT_HOST, USER_AGENT
from .errors import AuthExpiredError, NotLoggedInError, YuqueError
from .session import (
    CSRF_COOKIE,
    MODE_COOKIE,
    MODE_TOKEN,
    SESSION_COOKIE,
    Credentials,
    parse_cookie_string,
)


# ---------------------------------------------------------------- 内部工具
def _hostname(host: str) -> str:
    return urlsplit(host).hostname or host


def _domain_matches(cookie_domain: str, host: str) -> bool:
    d = cookie_domain.lstrip(".")
    return host == d or host.endswith("." + d)


def verify_cookie(host: str, cookies: dict[str, str]) -> dict | None:
    """用 Cookie 请求 ``/api/mine``；未登录/失效返回 ``None``。"""
    try:
        resp = httpx.get(
            f"{host.rstrip('/')}/api/mine",
            headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
            cookies=cookies,
            timeout=20,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 令牌登录
def login_token(
    token: str,
    *,
    host: str = DEFAULT_HOST,
    group: str = DEFAULT_GROUP,
    path: Path | None = None,
) -> Credentials:
    """用官方访问令牌登录（会先验证一次）。"""
    token = (token or "").strip()
    if not token:
        raise YuqueError("令牌为空")
    with YuqueApi(host, token, group=group) as api:
        api.hello()  # 验证令牌有效
        who = api.whoami()
        scopes = api.scopes
    cred = Credentials(
        mode=MODE_TOKEN,
        host=host.rstrip("/"),
        group=group,
        token=token,
        login=str(who.get("login") or ""),
        name=str(who.get("name") or ""),
        scopes=scopes,
    )
    # 团队令牌的 whoami 会返回所属团队：以服务端为准（否则会拿错 group 去查别的团队）
    if str(who.get("type") or "") == "Group" and who.get("login"):
        cred.group = str(who["login"])
    cred.save(path)
    return cred


# ---------------------------------------------------------------- Cookie 登录
def _credential_from_cookie(
    cookies: dict[str, str],
    *,
    host: str,
    group: str,
    who: dict,
    path: Path | None,
) -> Credentials:
    cred = Credentials(
        mode=MODE_COOKIE,
        host=host.rstrip("/"),
        group=group,
        cookies=cookies,
        login=str(who.get("login") or ""),
        name=str(who.get("name") or ""),
        scopes="cookie",
    )
    cred.save(path)
    return cred


def login_cookie_string(
    raw: str,
    *,
    host: str = DEFAULT_HOST,
    group: str = DEFAULT_GROUP,
    path: Path | None = None,
) -> Credentials:
    """直接粘贴 Cookie 串登录。"""
    cookies = parse_cookie_string(raw)
    if SESSION_COOKIE not in cookies:
        raise NotLoggedInError(
            f"Cookie 里缺少 {SESSION_COOKIE}（登录态）；请从浏览器复制完整 Cookie"
        )
    who = verify_cookie(host, cookies)
    if who is None:
        raise AuthExpiredError("Cookie 无效或已过期（/api/mine 未通过鉴权）")
    return _credential_from_cookie(cookies, host=host, group=group, who=who, path=path)


def login_browser(
    *,
    host: str = DEFAULT_HOST,
    group: str = DEFAULT_GROUP,
    browser: str = "auto",
    timeout: int = 300,
    path: Path | None = None,
) -> Credentials:
    """弹有头浏览器，等用户登录完成后抓取 Cookie。"""
    if browser not in BROWSER_CHOICES:
        raise YuqueError(f"--browser 只能是：{' / '.join(BROWSER_CHOICES)}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于本地是否装了 Playwright
        raise YuqueError(
            "未安装 Playwright，无法开浏览器登录。请任选其一：\n"
            '  1) 安装：`uv tool install "nju-yuque[login]"`；\n'
            "  2) 免浏览器：`yuque login --cookie '<粘贴浏览器 Cookie>'`"
        ) from exc

    login_url = f"{host.rstrip('/')}/login"
    hostname = _hostname(host)
    deadline = time.time() + max(timeout, 30)

    with sync_playwright() as p:
        browser_obj, _name = launch(p, browser, headless=False)
        context = browser_obj.new_context(locale="zh-CN")
        page = context.new_page()
        page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            while time.time() < deadline:
                cookies = {
                    c["name"]: c["value"]
                    for c in context.cookies()
                    if _domain_matches(str(c.get("domain", "")), hostname)
                }
                if SESSION_COOKIE in cookies:
                    who = verify_cookie(host, cookies)
                    if who is not None:
                        return _credential_from_cookie(
                            cookies, host=host, group=group, who=who, path=path
                        )
                page.wait_for_timeout(2000)
        finally:
            context.close()
            browser_obj.close()
    raise YuqueError(
        f"等待登录超时（{timeout}s）。请在打开的浏览器里完成登录；"
        "或改用 `yuque login --cookie '<Cookie>'`。"
    )


def cookie_has_csrf(cred: Credentials) -> bool:
    """Cookie 模式是否具备写操作能力（有 CSRF token）。"""
    return cred.is_cookie and CSRF_COOKIE in cred.cookies
