"""统一异常类型。

语雀官方接口出错时返回 ``{"status": 4xx, "message": "..."}``；
内部网页接口同样如此。这里把状态码收敛成固定异常，CLI 据此给出可操作提示。
"""

from __future__ import annotations


class YuqueError(RuntimeError):
    """所有语雀相关错误的基类。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotLoggedInError(YuqueError):
    """本地没有凭证，或凭证已被清空。"""


class AuthExpiredError(YuqueError):
    """凭证存在但服务端不认（401 / 登录态过期）。"""


class InsufficientScopeError(YuqueError):
    """令牌 scope 不足（403 / 「请给此 Token 添加 xxx 权限」）。"""


class NotFoundError(YuqueError):
    """实体不存在（404）。"""


class RateLimitedError(YuqueError):
    """触发限流（429）。"""


class WrongModeError(YuqueError):
    """当前登录模式不支持该操作（例如只读令牌想发评论）。"""


def raise_for_status(status: int, message: str) -> None:
    """把 HTTP 状态码翻译成具体异常。"""
    text = message or f"HTTP {status}"
    if status == 401:
        raise AuthExpiredError(text, status=status)
    if status == 403:
        raise InsufficientScopeError(text, status=status)
    if status == 404:
        raise NotFoundError(text, status=status)
    if status == 429:
        raise RateLimitedError(text, status=status)
    raise YuqueError(text, status=status)
