"""本地凭证：两种登录模式。

- ``token``：语雀官方 OpenAPI（``X-Auth-Token``）。零浏览器、只读为主，
  能不能写取决于令牌 scope。
- ``cookie``：网页登录态（``_yuque_session`` + ``yuque_ctoken``）。解锁评论 / 写文档，
  但属于非公开接口，且登录态会过期。

凭证文件 ``~/.yuque/auth.json`` 是敏感文件：**不读内容、不上传、不入库**。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import auth_file
from .errors import NotLoggedInError

SESSION_COOKIE = "_yuque_session"
CSRF_COOKIE = "yuque_ctoken"

MODE_TOKEN = "token"
MODE_COOKIE = "cookie"


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把 ``k=v; k2=v2`` 解析成 dict。"""
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


@dataclass
class Credentials:
    """一次登录的结果。"""

    mode: str
    host: str
    group: str = ""
    token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    login: str = ""  # 语雀 login
    name: str = ""  # 显示名
    scopes: str = ""  # 令牌 scope（服务端返回，便于 doctor 判断能力）
    created_at: str = ""

    # -- 便捷判断 ---------------------------------------------------------
    @property
    def is_token(self) -> bool:
        return self.mode == MODE_TOKEN

    @property
    def is_cookie(self) -> bool:
        return self.mode == MODE_COOKIE

    @property
    def csrf_token(self) -> str | None:
        return self.cookies.get(CSRF_COOKIE)

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def require_login(self) -> None:
        if self.is_token and not self.token:
            raise NotLoggedInError("尚未登录：请先运行 `yuque login --token <令牌>`")
        if self.is_cookie and SESSION_COOKIE not in self.cookies:
            raise NotLoggedInError("尚未登录：请先运行 `yuque login`")

    # -- 存取 -------------------------------------------------------------
    def save(self, path: Path | None = None) -> Path:
        target = path or auth_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        if not payload.get("created_at"):
            payload["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:  # Windows 下无 POSIX 权限位
            pass
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> Credentials:
        target = path or auth_file()
        if not target.exists():
            raise NotLoggedInError(
                "尚未登录：请先运行 `yuque login`（或 `yuque login --token <令牌>`）"
            )
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NotLoggedInError(f"凭证文件损坏：{target}（{exc}）") from exc
        cred = cls(
            mode=data.get("mode", MODE_TOKEN),
            host=data.get("host", ""),
            group=data.get("group", ""),
            token=data.get("token", ""),
            cookies=dict(data.get("cookies") or {}),
            login=data.get("login", ""),
            name=data.get("name", ""),
            scopes=data.get("scopes", ""),
            created_at=data.get("created_at", ""),
        )
        cred.require_login()
        return cred


def clear(path: Path | None = None) -> bool:
    """删除本地凭证，返回是否真的删掉了。"""
    target = path or auth_file()
    if target.exists():
        target.unlink()
        return True
    return False
