"""CLI 行为测试：全部离线，不触网。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nju_yuque.cli import app
from nju_yuque.session import MODE_TOKEN, Credentials

runner = CliRunner()


def test_doctor_without_login(isolated_home: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 2
    assert "尚未登录" in result.output


def test_doctor_with_token(isolated_home: Path, monkeypatch) -> None:
    cred = Credentials(
        mode=MODE_TOKEN,
        host="https://nova.yuque.com",
        group="ghxd00",
        token="t0k",
        scopes="group:read,repo:read,doc:read",
    )
    cred.save()

    class FakeApi:
        scopes = "group:read,repo:read,doc:read"

        def __init__(self, *a, **kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def hello(self):
            return "Hello NOVA"

        def whoami(self):
            return {"type": "Group"}

    monkeypatch.setattr("nju_yuque.cli.YuqueApi", FakeApi)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "token"
    assert payload["capabilities"]["read"] is True
    assert payload["capabilities"]["comment"] is False  # 官方接口没有评论能力


def test_logout(isolated_home: Path) -> None:
    Credentials(mode=MODE_TOKEN, host="h", token="t").save()
    assert runner.invoke(app, ["logout"]).exit_code == 0
    assert runner.invoke(app, ["logout"]).exit_code == 0  # 再删一次也不报错
