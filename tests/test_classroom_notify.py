"""通知层（文案 + 文件 outbox）单测。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from nju_yuque.classroom import notify as notify_mod
from nju_yuque.classroom.notify import (
    KIND_ACCEPTED,
    KIND_DELETED_REJECTED,
    KIND_DELETED_SUBMITTED,
    KIND_REJECTED,
    KIND_TAMPERED,
    KIND_UNRECOGNIZED,
    NOTICE_KINDS,
    ConsoleNotifier,
    FileOutboxNotifier,
    NoticeDoc,
    NoticeMember,
    NullNotifier,
    build_notifier,
    make_notice,
)

NOW = datetime(2026, 9, 12, 9, 0)


def notice(kind: str, **kw: object) -> notify_mod.Notice:
    doc = NoticeDoc(repo="g/r", doc_id=7, slug="s7", title="新生见面会", url="https://x/g/r/s7")
    member = NoticeMember(yuque_id=77, yuque_login="zhangsan", name="张三", applicant_raw="张三")
    return make_notice(
        seq=int(kw.pop("seq", 1)),  # type: ignore[arg-type]
        kind=kind,
        now=NOW,
        repo="g/r",
        doc=doc,
        member=member,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("kind", NOTICE_KINDS)
def test_every_kind_renders_a_message(kind: str) -> None:
    n = notice(kind, reasons=["原因一"], warnings=["提醒一"], date="2026-09-16", period="7-8")
    assert n.summary and "新生见面会" in n.summary
    assert n.message and "新生见面会" in n.message
    assert n.created_at.startswith("2026-09-12T09:00:00")
    assert n.schema_version == "1.0"


def test_rejected_message_lists_reasons_and_review_hint() -> None:
    n = notice(KIND_REJECTED, reasons=["活动时间不足 48 小时", "校区不认识"])
    assert "1. 活动时间不足 48 小时" in n.message
    assert "2. 校区不认识" in n.message
    assert "重新校验" in n.message


def test_accepted_message_mentions_lock() -> None:
    n = notice(KIND_ACCEPTED, date="2026-09-16", period="7-8", campus="仙林")
    assert "已生成教室借用申请" in n.message
    assert "不能再修改" in n.message


def test_tampered_and_deleted_messages() -> None:
    t = notice(KIND_TAMPERED, reasons=["活动时间：「16:10-18:00」→「17:00-19:00」"])
    assert "修改无效" in t.summary or "修改无效" in t.message
    assert "17:00-19:00" in t.message
    d = notice(KIND_DELETED_SUBMITTED)
    assert "不会撤回" in d.message
    r = notice(KIND_DELETED_REJECTED)
    assert "已经被删除" in r.message


def test_unrecognized_message_points_to_template() -> None:
    n = notice(KIND_UNRECOGNIZED, reasons=["提取不到日期/时间/校区"])
    assert "模板" in n.message


# ---------------------------------------------------------------- outbox
def test_outbox_writes_pending_and_audit_log(tmp_path: Path) -> None:
    box = FileOutboxNotifier(tmp_path / "notify")
    box.send(notice(KIND_REJECTED, seq=1))
    box.send(notice(KIND_ACCEPTED, seq=2))

    pendings = box.list_pending()
    # 文件名 = <seq>-<kind>-<doc_id>-<notice_id>.json（notice_id 保证不互相覆盖）
    assert [p.name.split("-")[0] for p in pendings] == ["000001", "000002"]
    assert [p.name.split("-")[1] for p in pendings] == ["rejected", "accepted"]
    assert all(p.name.endswith(".json") for p in pendings)
    assert len({p.name for p in pendings}) == 2
    assert not list((tmp_path / "notify").rglob("*.tmp"))
    # 审计流水按周存放
    log_file = next((tmp_path / "notify").rglob("outbox.jsonl"))
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert log_file.parent.name == "0907-0913"  # 事件的 created_at 属于哪一周就落哪一周
    assert json.loads(lines[0])["kind"] == KIND_REJECTED


def test_outbox_ack_moves_to_done(tmp_path: Path) -> None:
    box = FileOutboxNotifier(tmp_path / "notify")
    for seq in (1, 2, 3):
        box.send(notice(KIND_REJECTED, seq=seq))
    assert box.ack(999) is None
    moved = box.ack(2)
    assert moved is not None and moved.parent.name == "done"
    assert moved.parent.parent.name == "0907-0913"  # 挪到同一个周目录下的 done/
    assert [p.name.split("-")[0] for p in box.list_pending()] == ["000001", "000003"]
    assert len(box.ack_all()) == 2
    assert box.list_pending() == []
    assert box.purge_done() == 3
    assert box.purge_done() == 0


def test_outbox_list_pending_on_missing_dir(tmp_path: Path) -> None:
    assert FileOutboxNotifier(tmp_path / "nope").list_pending() == []


# ---------------------------------------------------------------- 工厂
def test_build_notifier() -> None:
    assert isinstance(build_notifier("none", "."), NullNotifier)
    assert isinstance(build_notifier("console", "."), ConsoleNotifier)
    assert isinstance(build_notifier("outbox", "x"), FileOutboxNotifier)
    combo = build_notifier("outbox,console", "x")
    assert isinstance(combo, notify_mod.CompositeNotifier)
    with pytest.raises(ValueError):
        build_notifier("telegram", ".")


def test_composite_fans_out(tmp_path: Path) -> None:
    box = FileOutboxNotifier(tmp_path / "n")
    combo = notify_mod.CompositeNotifier([box, NullNotifier()])
    combo.send(notice(KIND_ACCEPTED))
    assert len(box.list_pending()) == 1


def test_console_notifier_writes_message() -> None:
    import io

    buf = io.StringIO()
    ConsoleNotifier(buf).send(notice(KIND_ACCEPTED))
    assert "新生见面会" in buf.getvalue()


def test_seq_reuse_does_not_overwrite_pending(tmp_path: Path) -> None:
    """state.json 被重置后 seq 会从 1 重来：不同 notice_id 必须是两个文件。"""
    box = FileOutboxNotifier(tmp_path / "notify")
    first = notice(KIND_REJECTED, seq=1)
    second = notice(KIND_REJECTED, seq=1)
    assert first.notice_id != second.notice_id
    box.send(first)
    box.send(second)
    assert len(box.list_pending()) == 2
    assert len(box.ack_all()) == 2


def test_events_land_in_the_week_of_their_own_created_at(tmp_path: Path) -> None:
    """跨周写入的事件不能串周：各自落在自己那一周。"""

    box = FileOutboxNotifier(tmp_path / "notify")
    old = notice(KIND_REJECTED, seq=1, **{})
    old = old.model_copy(update={"created_at": "2026-09-06T23:59:00+08:00"})
    new = notice(KIND_ACCEPTED, seq=2).model_copy(
        update={"created_at": "2026-09-07T00:01:00+08:00"}
    )
    box.send(old)
    box.send(new)
    assert [p.parent.parent.name for p in box.list_pending()] == ["0831-0906", "0907-0913"]
    # 消费顺序：先旧后新
    assert [p.name.split("-")[0] for p in box.list_pending()] == ["000001", "000002"]


def test_migrate_flat_layout(tmp_path: Path) -> None:
    """老扁平布局（notify/pending）要能自动搬进按周目录，一条都不能丢。"""
    root = tmp_path / "notify"
    (root / "pending").mkdir(parents=True)
    first = notice(KIND_REJECTED, seq=1).model_copy(
        update={"created_at": "2026-09-12T09:00:00+08:00"}
    )
    second = notice(KIND_ACCEPTED, seq=2).model_copy(
        update={"created_at": "2026-09-19T09:00:00+08:00"}
    )
    for n in (first, second):
        (root / "pending" / f"{n.seq:06d}-{n.kind}-{n.doc.doc_id}-{n.notice_id}.json").write_text(
            n.to_json(), encoding="utf-8"
        )
    (root / "outbox.jsonl").write_text(first.to_json().strip() + chr(10), encoding="utf-8")

    box = FileOutboxNotifier(root)
    assert len(box.list_pending()) == 2  # 迁移前也能看到（兼容期）
    moved = box.migrate_flat_layout()
    assert moved
    assert not (root / "pending").exists() and not (root / "outbox.jsonl").exists()
    pendings = box.list_pending()
    assert len(pendings) == 2
    assert sorted(p.parent.parent.name for p in pendings) == ["0907-0913", "0914-0920"]
    assert box.migrate_flat_layout() == []  # 幂等
