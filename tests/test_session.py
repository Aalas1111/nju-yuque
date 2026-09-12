"""凭证存取与 Cookie 解析测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from nju_yuque.errors import NotLoggedInError
from nju_yuque.session import (
    MODE_COOKIE,
    MODE_TOKEN,
    Credentials,
    clear,
    parse_cookie_string,
    scope_allows_write,
    write_kinds,
)


def test_parse_cookie_string() -> None:
    raw = "a=1; _yuque_session=abc;  yuque_ctoken = t0k ;broken"
    out = parse_cookie_string(raw)
    assert out["_yuque_session"] == "abc"
    assert out["yuque_ctoken"] == "t0k"
    assert "broken" not in out


def test_token_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    cred = Credentials(mode=MODE_TOKEN, host="https://nova.yuque.com", group="ghxd00", token="t0k")
    cred.save(path)
    loaded = Credentials.load(path)
    assert loaded.mode == MODE_TOKEN
    assert loaded.token == "t0k"
    assert loaded.group == "ghxd00"
    assert loaded.created_at  # save 时自动补


def test_cookie_roundtrip_and_csrf(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    cred = Credentials(
        mode=MODE_COOKIE,
        host="https://nova.yuque.com",
        cookies={"_yuque_session": "s", "yuque_ctoken": "c"},
    )
    cred.save(path)
    loaded = Credentials.load(path)
    assert loaded.is_cookie
    assert loaded.csrf_token == "c"
    assert loaded.cookie_header == "_yuque_session=s; yuque_ctoken=c"


def test_empty_credentials_rejected() -> None:
    with pytest.raises(NotLoggedInError):
        Credentials(mode=MODE_TOKEN, host="x", token="").require_login()
    with pytest.raises(NotLoggedInError):
        Credentials(mode=MODE_COOKIE, host="x", cookies={}).require_login()


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NotLoggedInError):
        Credentials.load(tmp_path / "nope.json")


def test_clear(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text("{}", encoding="utf-8")
    assert clear(path) is True
    assert clear(path) is False


def test_scope_allows_write() -> None:
    read_only = "group:read,repo:read,doc:read,statistic:read,private_search"
    assert scope_allows_write(read_only, "doc") is False
    assert scope_allows_write(read_only, "repo") is False

    writable = "group,repo,doc,statistic:read,private_search"
    assert scope_allows_write(writable, "doc") is True
    assert scope_allows_write(writable, "repo") is True
    assert scope_allows_write(writable, "statistic") is False

    assert scope_allows_write("doc:write", "doc") is True
    assert scope_allows_write("", "doc") is False
    assert scope_allows_write("docextra:read", "doc") is False


def test_write_kinds() -> None:
    assert write_kinds("group,repo,doc,statistic:read") == ["doc", "repo", "group"]
    assert write_kinds("group:read,repo:read,doc:read") == []
