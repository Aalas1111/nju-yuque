"""``yuque classroom`` 子命令单测（只测本地命令；联网命令见 pipeline 测试）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from nju_yuque.classroom import cli as classroom_cli
from nju_yuque.classroom import notify as notify_mod
from nju_yuque.classroom.notify import KIND_ACCEPTED, KIND_REJECTED
from nju_yuque.classroom.store import STATUS_SUBMITTED, DocState, Store
from nju_yuque.cli import app

runner = CliRunner()


def test_classroom_is_mounted() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "classroom" in result.output
    sub = runner.invoke(app, ["classroom", "--help"])
    assert sub.exit_code == 0
    for command in ("once", "run", "serve", "status", "outbox", "parse", "schema", "rules"):
        assert command in sub.output


def test_rules_command() -> None:
    result = runner.invoke(app, ["classroom", "rules"])
    assert result.exit_code == 0
    assert "48" in result.output
    assert "草稿" in result.output
    assert "16:10" in result.output  # 节次表


def test_schema_command_to_stdout() -> None:
    result = runner.invoke(app, ["classroom", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["$id"].endswith("1.0")


def test_schema_command_to_file(tmp_path: Path) -> None:
    target = tmp_path / "schema.json"
    result = runner.invoke(app, ["classroom", "schema", "--out", str(target)])
    assert result.exit_code == 0 and target.is_file()


def test_parse_command(tmp_path: Path, isolated_home: Path) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"action_type": "publish", "data": {"id": 42, "title": "读书会"}}),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["classroom", "parse", str(payload), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {
        "action": "upsert",
        "doc_id": 42,
        "title": "读书会",
        "slug": "",
        "repo": "",
        "action_type": "publish",
        "usable": True,
    }


def test_parse_command_rejects_bad_file(tmp_path: Path) -> None:
    broken = tmp_path / "bad.json"
    broken.write_text("{", encoding="utf-8")
    assert runner.invoke(app, ["classroom", "parse", str(broken)]).exit_code == 1
    assert runner.invoke(app, ["classroom", "parse", str(tmp_path / "gone.json")]).exit_code == 1


# ---------------------------------------------------------------- status / outbox
def _seed(outdir: Path) -> Store:
    store = Store(outdir / "state.json", "g/r")
    store.put(
        DocState(
            doc_id=1,
            title="新生见面会",
            status=STATUS_SUBMITTED,
            application_id="2026-09-16-7_8-1",
            problems=[],
        )
    )
    store.put(DocState(doc_id=2, title="读书会", status="rejected", problems=["「校区」为空"]))
    store.meta.rounds = 2
    store.save()
    return store


def test_status_command(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    _seed(outdir)
    result = runner.invoke(
        app, ["classroom", "status", "--repo", "g/r", "--outdir", str(outdir), "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["counts"][STATUS_SUBMITTED] == 1
    assert data["counts"]["rejected"] == 1
    assert data["meta"]["rounds"] == 2
    assert data["docs"][0]["application_id"] == "2026-09-16-7_8-1"

    human = runner.invoke(app, ["classroom", "status", "--repo", "g/r", "--outdir", str(outdir)])
    assert human.exit_code == 0
    assert "新生见面会" in human.output and "已受理" in human.output


def test_status_on_empty_outdir(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["classroom", "status", "--repo", "g/r", "--outdir", str(tmp_path / "new")]
    )
    assert result.exit_code == 0


def test_outbox_list_ack_purge(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    box = notify_mod.FileOutboxNotifier(outdir / "notify")
    from nju_yuque.classroom.notify import NoticeDoc, NoticeMember, make_notice

    for seq, kind in ((1, KIND_ACCEPTED), (2, KIND_REJECTED)):
        box.send(
            make_notice(
                seq=seq,
                kind=kind,
                now=__import__("datetime").datetime(2026, 9, 12, 9, 0),
                repo="g/r",
                doc=NoticeDoc(repo="g/r", doc_id=seq, title=f"活动{seq}", url=""),
                member=NoticeMember(name="张三"),
                reasons=["原因"],
            )
        )

    listed = runner.invoke(
        app, ["classroom", "outbox", "list", "--repo", "g/r", "--outdir", str(outdir), "--json"]
    )
    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert [r["kind"] for r in rows] == [KIND_ACCEPTED, KIND_REJECTED]

    human = runner.invoke(
        app, ["classroom", "outbox", "list", "--repo", "g/r", "--outdir", str(outdir)]
    )
    assert human.exit_code == 0 and "活动1" in human.output

    acked = runner.invoke(
        app,
        [
            "classroom",
            "outbox",
            "ack",
            "--seq",
            "1",
            "--repo",
            "g/r",
            "--outdir",
            str(outdir),
            "--json",
        ],
    )
    acked_names = json.loads(acked.output)["acknowledged"]
    assert acked.exit_code == 0 and len(acked_names) == 1
    assert acked_names[0].startswith("000001-accepted-1-")
    assert len(box.list_pending()) == 1

    rest = runner.invoke(
        app, ["classroom", "outbox", "ack", "--repo", "g/r", "--outdir", str(outdir), "--json"]
    )
    assert rest.exit_code == 0 and len(box.list_pending()) == 0

    purge = runner.invoke(
        app, ["classroom", "outbox", "purge", "--repo", "g/r", "--outdir", str(outdir)]
    )
    assert purge.exit_code == 0 and box.purge_done() == 0


def test_repo_option_required() -> None:
    result = runner.invoke(app, ["classroom", "status"], env={"YUQUE_CLASSROOM_REPO": ""})
    assert result.exit_code == 1 and "缺少 --repo" in result.output


def test_repo_from_env(tmp_path: Path, isolated_home: Path) -> None:
    result = runner.invoke(
        app, ["classroom", "status", "--json"], env={"YUQUE_CLASSROOM_REPO": "g/r"}
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["repo"] == "g/r"


def test_outdir_belongs_to_another_repo(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    _seed(outdir)  # 里面记的是 g/r
    result = runner.invoke(
        app, ["classroom", "status", "--repo", "other/repo", "--outdir", str(outdir)]
    )
    assert result.exit_code == 1 and "另一个知识库" in result.output


def test_once_requires_token_login(tmp_path: Path, isolated_home: Path) -> None:
    result = runner.invoke(
        app, ["classroom", "once", "--repo", "g/r", "--outdir", str(tmp_path / "out")]
    )
    assert result.exit_code == 2
    assert "登录" in result.output


def test_kinds_option_parsing() -> None:
    from nju_yuque.classroom.notify import NOTICE_KINDS

    assert classroom_cli._kinds(None) == NOTICE_KINDS
    assert classroom_cli._kinds(["none"]) == ()
    assert classroom_cli._kinds(["accepted,rejected"]) == ("accepted", "rejected")
    with pytest.raises(typer.Exit):
        classroom_cli._kinds(["bogus"])


# ---------------------------------------------------------------- once 端到端（假语雀）
def _future_date(days: int = 4) -> str:
    """测试用「未来的日期」：写死日期会随运行日期过期。"""
    from datetime import datetime, timedelta

    from nju_yuque.classroom import rules

    return (datetime.now(rules.CN) + timedelta(days=days)).strftime("%Y-%m-%d")


class _FakeYuqueApi:
    """替掉真 API：只实现 classroom 需要的三个只读方法。"""

    docs_store: list = []
    items_store: list = []
    fail = False

    def __init__(self, host: str = "", token: str = "", *, group: str = "") -> None:
        self.host, self.token, self.group = host, token, group

    def __enter__(self) -> _FakeYuqueApi:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        return None

    def toc(self, _repo: str) -> list:
        return list(type(self).items_store)

    def docs(self, _repo: str) -> list:
        if type(self).fail:
            from nju_yuque.errors import YuqueError

            raise YuqueError("模拟：知识库读取失败")
        return list(type(self).docs_store)

    def doc(self, _repo: str, slug: str):
        if type(self).fail:
            from nju_yuque.errors import YuqueError

            raise YuqueError("模拟：文档读取失败")
        return next(d for d in type(self).docs_store if str(d.id) == str(slug))


def _login(isolated_home: Path) -> None:
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "auth.json").write_text(
        json.dumps(
            {
                "mode": "token",
                "host": "https://nova.yuque.com",
                "group": "lqogh0",
                "token": "dummy-token",
                "cookies": {},
                "login": "me",
                "name": "me",
                "scopes": "doc:read",
                "created_at": "2026-09-12T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _expected_id(date: str) -> str:
    return f"{date}-7_8-1"


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYuqueApi]:
    import classroom_fakes  # type: ignore[import-not-found]

    _FakeYuqueApi.docs_store = [
        classroom_fakes.make_doc(1, "新生见面会", classroom_fakes.body(活动日期=_future_date()))
    ]
    _FakeYuqueApi.items_store = [classroom_fakes.toc_doc("n1", 1, "新生见面会")]
    _FakeYuqueApi.fail = False
    monkeypatch.setattr(classroom_cli, "YuqueApi", _FakeYuqueApi)
    return _FakeYuqueApi


def test_once_end_to_end_and_dry_run(tmp_path: Path, isolated_home: Path, fake_api: type) -> None:
    _login(isolated_home)
    outdir = tmp_path / "out"

    dry = runner.invoke(
        app,
        [
            "classroom",
            "once",
            "--repo",
            "lqogh0/jsjysq",
            "--outdir",
            str(outdir),
            "--dry-run",
            "--json",
        ],
    )
    assert dry.exit_code == 0
    payload = json.loads(dry.output)
    expected = _expected_id(_future_date())
    assert payload["accepted"] == [expected]
    assert payload["dry_run"] is True
    assert not (outdir / "state.json").exists()  # dry-run 什么都不落盘
    assert not (outdir / "applications").exists()

    real = runner.invoke(
        app,
        ["classroom", "once", "--repo", "lqogh0/jsjysq", "--outdir", str(outdir), "--json"],
    )
    assert real.exit_code == 0
    payload = json.loads(real.output)
    assert payload["accepted"] == [expected]
    assert (outdir / "state.json").is_file()
    app_file = outdir / "applications" / f"{expected}.json"
    assert app_file.is_file()
    assert json.loads(app_file.read_text(encoding="utf-8"))["activity"]["campus"] == "仙林"
    assert list((outdir / "notify").rglob("pending/*.json"))  # 通知按周分组

    # 人类可读模式
    human = runner.invoke(
        app, ["classroom", "once", "--repo", "lqogh0/jsjysq", "--outdir", str(outdir)]
    )
    assert human.exit_code == 0
    assert "已受理" in human.output or "知识库 1 篇" in human.output


def test_once_exit_code_reflects_read_failures(
    tmp_path: Path, isolated_home: Path, fake_api: type
) -> None:
    """读取失败时必须非 0 退出（cron / 脚本要能感知），--json 也一样。"""
    _login(isolated_home)
    fake_api.fail = True
    outdir = tmp_path / "out"

    json_run = runner.invoke(
        app,
        ["classroom", "once", "--repo", "lqogh0/jsjysq", "--outdir", str(outdir), "--json"],
    )
    assert json_run.exit_code == 1
    assert json.loads(json_run.output)["errors"]

    human = runner.invoke(
        app, ["classroom", "once", "--repo", "lqogh0/jsjysq", "--outdir", str(outdir)]
    )
    assert human.exit_code == 1
    assert "✗" in human.output


def test_once_rejects_bad_default_people(
    tmp_path: Path, isolated_home: Path, fake_api: type
) -> None:
    _login(isolated_home)
    result = runner.invoke(
        app,
        [
            "classroom",
            "once",
            "--repo",
            "lqogh0/jsjysq",
            "--outdir",
            str(tmp_path / "out"),
            "--default-people",
            "0",
        ],
    )
    assert result.exit_code == 2 and "必须大于 0" in result.output


def test_once_reports_corrupt_state_file(
    tmp_path: Path, isolated_home: Path, fake_api: type
) -> None:
    _login(isolated_home)
    outdir = tmp_path / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "state.json").write_text("{ broken", encoding="utf-8")
    result = runner.invoke(
        app, ["classroom", "once", "--repo", "lqogh0/jsjysq", "--outdir", str(outdir)]
    )
    assert result.exit_code == 1 and "损坏" in result.output


def test_once_can_disable_some_notifications(
    tmp_path: Path, isolated_home: Path, fake_api: type
) -> None:
    _login(isolated_home)
    import classroom_fakes  # type: ignore[import-not-found]

    fake_api.docs_store = [
        classroom_fakes.make_doc(1, text=classroom_fakes.body(活动日期=_future_date(), 校区="火星"))
    ]
    outdir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "classroom",
            "once",
            "--repo",
            "lqogh0/jsjysq",
            "--outdir",
            str(outdir),
            "--notify-kinds",
            "accepted",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["rejected"] == ["新生见面会"]
    assert not list((outdir / "notify").rglob("pending/*.json"))  # 没发通知
    assert (outdir / "state.json").is_file()  # 但状态照样记了
