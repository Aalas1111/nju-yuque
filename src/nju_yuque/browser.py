"""浏览器启动辅助（仅在 ``yuque login`` 抓 cookie 时使用）。

优先用 Playwright 自带 Chromium，没有就回退系统 Edge / Chrome（免下载内核）。
"""

from __future__ import annotations


def launch(p, browser: str = "auto", headless: bool = False):
    """启动浏览器，返回 ``(browser 实例, 实际使用的名字)``。"""
    from playwright.sync_api import Error as PWError

    order = ["chromium", "msedge", "chrome"] if browser == "auto" else [browser]
    errors: list[str] = []
    for name in order:
        try:
            if name == "chromium":
                return p.chromium.launch(headless=headless), name
            return p.chromium.launch(headless=headless, channel=name), name
        except PWError as exc:
            errors.append(f"{name}: {str(exc).splitlines()[0]}")
    raise RuntimeError(
        "无法启动任何浏览器，请任选其一：\n"
        "  1) 安装系统 Edge / Chrome（推荐，免下载）；\n"
        "  2) 下载 Playwright Chromium：`uv run playwright install chromium`\n"
        "已尝试：\n  " + "\n  ".join(errors)
    )
