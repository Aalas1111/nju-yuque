"""pipeline（一轮处理）的行为测试：受理 / 退回 / 改动告警 / 删除 / 草稿 / 结构性跳过。

全部离线：假语雀 + 真 store + 真文件 outbox。
"""

from __future__ import annotations

import json
from pathlib import Path

from classroom_fakes import (  # type: ignore[import-not-found]
    HOST,
    REPO,
    body,
    build_harness,
    draft_body,
    make_doc,
    toc_doc,
    toc_title,
)

from nju_yuque.classroom import store as store_mod
from nju_yuque.classroom.notify import (
    KIND_ACCEPTED,
    KIND_DELETED_REJECTED,
    KIND_DELETED_SUBMITTED,
    KIND_REJECTED,
    KIND_TAMPERED,
    KIND_UNRECOGNIZED,
)

FULL = "2026-09-12T08:00:00+08:00"


# ---------------------------------------------------------------- 受理
def test_first_round_accepts_and_writes_application(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "新生见面会")])
    report = h.run()

    assert report.snapshot_ok and report.accepted == ["2026-09-16-7_8-1"]
    assert h.last.rejected == [] and h.last.notified[0].kind == KIND_ACCEPTED

    apps = h.applications()
    assert len(apps) == 1
    app = apps[0]
    assert app["schema_version"] == "1.0"
    assert app["application_type"] == "classroom_borrow"
    assert app["activity"] == {
        "name": "新生见面会",
        "date": "2026-09-16",
        "start": "16:10",
        "end": "18:00",
        "period": "7-8",
        "period_start": 7,
        "period_end": 8,
        "campus": "仙林",
        "campus_code": "3",
        "building": "",
        "room": "",
        "people": 30,
        "people_source": "default",
        "borrow_type": "团学活动",
    }
    assert app["source"]["doc_id"] == 1
    assert app["source"]["doc_url"] == f"{HOST}/{REPO}/s1"
    assert app["source"]["creator_name"] == "张三"
    assert app["source"]["content_hash"].startswith("sha256:")
    assert app["raw_fields"]["活动时间"] == "16:10-18:00"

    idx = h.index()
    assert idx["counts"][store_mod.STATUS_SUBMITTED] == 1
    assert idx["generated_at"] == h.state_file()["meta"]["last_round_at"] != ""
    assert idx["applications"][0]["application_id"] == "2026-09-16-7_8-1"
    assert idx["applications"][0]["activity"]["period"] == "7-8"

    state = h.state_file()["docs"]["1"]
    assert state["status"] == store_mod.STATUS_SUBMITTED
    assert state["application_file"].endswith("2026-09-16-7_8-1.json")
    assert h.state_file()["meta"]["rounds"] == 1


def test_second_round_is_quiet(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()
    report = h.run()
    assert report.accepted == [] and report.notified == []
    assert dict(report.skipped) == {"已提交·未变动": 1}
    assert h.state_file()["meta"]["rounds"] == 2


def test_normalized_application_records_fixes(tmp_path: Path) -> None:
    h = build_harness(
        tmp_path,
        [make_doc(1, "读书会", body(活动日期="9月16日", 活动时间="下午4点-6点", 校区="仙林校区"))],
    )
    h.run()
    app = h.applications()[0]
    assert app["activity"]["date"] == "2026-09-16"
    assert any("日期" in f for f in app["normalizations"])
    assert app["warnings"] == []


def test_applicant_inferred_from_creator(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, text=body(申请人=""))])
    h.run()
    assert h.applications()[0]["source"]["applicant"] == "张三"
    assert h.applications()[0]["source"]["applicant_raw"] == ""


# ---------------------------------------------------------------- 退回
def test_rejected_doc_notifies_once_and_rechecks_after_edit(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "新生见面会", body(校区="火星"))])
    first = h.run()
    assert first.rejected == ["新生见面会"]
    notices = h.notices(KIND_REJECTED)
    assert len(notices) == 1
    assert "不认识" in notices[0]["message"]
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_REJECTED
    assert h.applications() == []

    # 同一份文档、同样的问题，再跑一轮 → 不重复打扰
    h.box.ack_all()
    h.run()
    assert h.notices() == []

    # 社员改好了 → 自动重新受理（不需要他做别的动作）
    h.source.edit(1, body(校区="仙林"), ts="2026-09-12T10:00:00+08:00")
    third = h.run()
    assert third.accepted == ["2026-09-16-7_8-1"]
    assert [n["kind"] for n in h.notices()] == [KIND_ACCEPTED]


def test_same_rejection_reason_is_not_renotified_but_new_reason_is(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, text=body(校区="火星", 人数="很多"))])
    h.run()
    assert len(h.notices(KIND_REJECTED)) == 1
    h.box.ack_all()

    # 只改了无关的正文（问题没变）→ 指纹一样，不重复通知
    h.source.edit(
        1, body(校区="火星", 人数="很多") + "备注：随便加一句\n", ts="2026-09-12T10:00:00+08:00"
    )
    h.run()
    assert h.notices(KIND_REJECTED) == []

    # 问题变了 → 重新通知
    h.source.edit(1, body(校区="火星"), ts="2026-09-12T11:00:00+08:00")
    h.run()
    assert len(h.notices(KIND_REJECTED)) == 1


def test_unrecognized_doc(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "随笔", "今天天气不错，随便写点东西。")])
    report = h.run()
    assert report.unrecognized == ["随笔"] and report.rejected == []
    notices = h.notices(KIND_UNRECOGNIZED)
    assert len(notices) == 1 and notices[0]["doc"]["title"] == "随笔"
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_UNRECOGNIZED


def test_freeform_body_is_salvaged(tmp_path: Path) -> None:
    h = build_harness(
        tmp_path,
        [make_doc(1, "社团分享会", "下周三 9月16日 下午 16:00-18:00 在仙林借个教室，社团分享会")],
    )
    report = h.run()
    assert report.accepted == ["2026-09-16-7_8-1"]
    assert any("未使用模板" in f for f in h.applications()[0]["normalizations"])


# ---------------------------------------------------------------- 草稿
def test_draft_docs_are_never_recorded(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "新生见面会", draft_body())])
    report = h.run()
    assert dict(report.skipped) == {"草稿": 1}
    assert h.state_file()["docs"] == {}
    assert h.applications() == [] and h.notices() == []

    # 删掉草稿标记 → 立刻受理
    h.source.edit(1, body(), ts="2026-09-12T10:00:00+08:00")
    assert h.run().accepted == ["2026-09-16-7_8-1"]


def test_adding_draft_marker_back_clears_local_record(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, text=body(校区="火星"))])
    h.run()
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_REJECTED

    h.source.edit(1, draft_body(校区="火星"), ts="2026-09-12T10:00:00+08:00")
    report = h.run()
    assert report.dropped_drafts == ["新生见面会"]
    assert h.state_file()["docs"] == {}


def test_draft_marker_cannot_reset_a_submitted_doc(tmp_path: Path) -> None:
    """已提交就是终态：加回草稿标记不能当「撤回」用。"""
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()
    h.source.edit(1, draft_body(), ts="2026-09-12T10:00:00+08:00")
    h.run()
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED
    assert h.notices(KIND_TAMPERED), "改了内容（哪怕只是加了草稿标记）也要告警"


# ---------------------------------------------------------------- 改动告警
def test_tampering_notifies_once_per_change(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()

    h.source.edit(1, body(活动时间="17:00-19:00"), ts="2026-09-12T10:00:00+08:00")
    h.run()
    notices = h.notices(KIND_TAMPERED)
    assert len(notices) == 1
    assert notices[0]["doc"]["doc_id"] == 1
    assert any("17:00-19:00" in r for r in notices[0]["reasons"]), notices[0]["reasons"]
    assert "无效" in notices[0]["message"]

    # 内容没再动 → 不重复提醒
    h.box.ack_all()
    h.run()
    assert h.notices() == []

    # 又改了一次 → 再提醒一次
    h.source.edit(1, body(活动时间="18:00-20:00"), ts="2026-09-12T11:00:00+08:00")
    h.run()
    assert len(h.notices(KIND_TAMPERED)) == 1


def test_title_change_is_tampering(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "新生见面会")])
    h.run()
    h.box.ack_all()
    h.source.edit(1, body(), title="新生见面会（改）", ts="2026-09-12T10:00:00+08:00")
    h.run()
    notices = h.notices(KIND_TAMPERED)
    assert notices and any("标题" in r for r in notices[0]["reasons"])


def test_metadata_only_change_is_not_tampering(tmp_path: Path) -> None:
    """updated_at 变了但正文没变（比如只改了个标点又被撤回）→ 不打扰。"""
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()
    h.source.by_id[1] = h.source.by_id[1].model_copy(
        update={"updated_at": "2026-09-12T10:00:00+08:00"}
    )
    h.run()
    assert h.notices() == []


# ---------------------------------------------------------------- 删除
def test_deleted_submitted_doc_warns_about_withdrawal(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()

    h.source.remove(1)
    report = h.run()  # 第 1 轮：疑似消失，等确认（默认 grace=2 轮）
    assert report.deleted == [] and h.state_file()["docs"]["1"]["missing_rounds"] == 1

    report = h.run()  # 第 2 轮：确认删除
    assert report.deleted == ["新生见面会"]
    notices = h.notices(KIND_DELETED_SUBMITTED)
    assert len(notices) == 1 and notices[0]["extra"]["detected_by"] == "poll"
    entry = h.state_file()["docs"]["1"]
    assert entry["deleted_at"]  # 默认留墓碑（不是删记录）
    assert h.state_file()["docs"] and h.store.counts()["deleted"] == 1


def test_deleted_rejected_doc_notifies_and_clears(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, text=body(校区="火星"))])
    h.run()
    h.box.ack_all()
    h.source.remove(1)
    h.run()
    h.run()
    notices = h.notices(KIND_DELETED_REJECTED)
    assert len(notices) == 1
    entry = h.state_file()["docs"]["1"]
    assert entry["status"] == store_mod.STATUS_REJECTED and entry["deleted_at"]


def test_purge_deleted_drops_the_record(tmp_path: Path) -> None:
    """显式要求「删干净」时才真的删记录。"""
    h = build_harness(tmp_path, [make_doc(1)], purge_deleted=True)
    h.run()
    h.source.remove(1)
    h.run()
    h.run()
    assert h.state_file()["docs"] == {}


def test_delete_grace_rounds(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)], delete_grace_rounds=3)
    h.run()
    h.source.remove(1)
    assert h.run().deleted == []
    assert h.run().deleted == []
    assert h.run().deleted == ["新生见面会"]
    assert h.state_file()["docs"]["1"]["deleted_at"]


def test_delete_grace_one_is_immediate(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)], delete_grace_rounds=1)
    h.run()
    h.source.remove(1)
    assert h.run().deleted == ["新生见面会"]


def test_webhook_delete_is_immediate(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()
    h.source.remove(1)  # webhook 事件也要能读出 404 才认（见 test_webhook_delete_is_ignored...）
    notice = h.pipeline.handle_delete(1)
    assert notice is not None and notice.kind == KIND_DELETED_SUBMITTED
    assert len(h.notices(KIND_DELETED_SUBMITTED)) == 1
    assert h.state_file()["docs"]["1"]["deleted_at"]
    assert h.pipeline.handle_delete(1) is None  # 幂等


def test_failed_snapshot_does_not_report_deletions(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.source.fail = True
    report = h.run()
    assert not report.snapshot_ok and report.errors
    assert (
        report.deleted == [] and h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED
    )


# ---------------------------------------------------------------- 结构性跳过
def test_guide_doc_is_skipped_by_locked_id(tmp_path: Path) -> None:
    items = [
        toc_doc("n1", 1, "指导文档（必读）", slug="guide"),
        toc_doc("n2", 2, "新生见面会", slug="s2"),
    ]
    guide = make_doc(1, "指导文档（必读）", "这是填表说明", slug="guide")
    h = build_harness(tmp_path, [guide, make_doc(2)], items=items)
    report = h.run()
    assert report.notes and "指导文档" in report.notes[0]
    assert "1" not in h.state_file()["docs"]  # 指导文档不进状态
    assert report.accepted == ["2026-09-16-7_8-2"]
    assert h.state_file()["meta"]["guide_doc_id"] == 1

    # 第二轮：按 id 锁定，即使标题被改也不处理
    h.source.edit(1, "说明改了", title="随便什么标题", ts="2026-09-12T10:00:00+08:00")
    h.run()
    assert "1" not in h.state_file()["docs"]


def test_explicit_guide_option(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1, "随便的名字", "不是申请")], guide="1")
    report = h.run()
    assert report.unrecognized == [] and "1" not in h.state_file()["docs"]


def test_archived_docs_are_never_touched(tmp_path: Path) -> None:
    items = [
        toc_title("arch", "归档区"),
        toc_title("week", "0907-0913", parent="arch"),
        toc_doc("n1", 1, "上周例会", parent="week"),
    ]
    h = build_harness(tmp_path, [make_doc(1, "上周例会", "随便写写")], items=items)
    report = h.run()
    assert dict(report.skipped) == {"已归档": 1}
    assert h.notices() == [] and h.state_file()["docs"] == {}


def test_scope_limits_processing(tmp_path: Path) -> None:
    items = [
        toc_title("apply", "申请表"),
        toc_doc("n1", 1, "新生见面会", parent="apply"),
        toc_doc("n2", 2, "别的组的文档"),
    ]
    h = build_harness(
        tmp_path,
        [make_doc(1), make_doc(2, "别的组的文档", "随便写")],
        items=items,
        scope_title="申请表",
    )
    report = h.run()
    assert report.accepted == ["2026-09-16-7_8-1"]
    assert dict(report.skipped) == {"不在「申请表」目录内": 1}


def test_legacy_approval_log_is_skipped(tmp_path: Path) -> None:
    items = [
        toc_doc("n1", 1, "新生见面会"),
        toc_doc("n2", 2, "审批日志", parent="n1"),
    ]
    h = build_harness(tmp_path, [make_doc(1), make_doc(2, "审批日志", "旧版遗留")], items=items)
    report = h.run()
    assert dict(report.skipped) == {"审批日志（旧版遗留）": 1}
    assert h.state_file()["docs"].get("2") is None


def test_sheet_docs_are_skipped(tmp_path: Path) -> None:
    doc = make_doc(1, "表格", "x", type_="Sheet")
    h = build_harness(tmp_path, [doc])
    report = h.run()
    assert dict(report.skipped) == {"非普通文档（Sheet）": 1}


# ---------------------------------------------------------------- dry-run / 通知开关
def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1), make_doc(2, text=body(校区="火星"))], dry_run=True)
    report = h.run()
    assert report.dry_run and report.accepted == ["2026-09-16-7_8-1"]
    assert [n.kind for n in report.notified] == [KIND_ACCEPTED, KIND_REJECTED]
    assert not (h.outdir / "state.json").exists()
    assert not (h.outdir / "applications").exists()
    assert h.notices() == []


def test_notify_kinds_can_be_disabled(tmp_path: Path) -> None:
    h = build_harness(
        tmp_path, [make_doc(1), make_doc(2, text=body(校区="火星"))], notify_kinds=(KIND_ACCEPTED,)
    )
    h.run()
    assert [n["kind"] for n in h.notices()] == [KIND_ACCEPTED]
    assert h.state_file()["docs"]["2"]["status"] == store_mod.STATUS_REJECTED


def test_force_rereads_unchanged_docs(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    reads = len(h.source.reads)
    h.run()
    assert len(h.source.reads) == reads  # 没变动就不读正文
    h.run(force={1})
    assert len(h.source.reads) > reads


def test_report_serialises_to_json(tmp_path: Path) -> None:
    h = build_harness(tmp_path, [make_doc(1)])
    payload = json.loads(json.dumps(h.run().to_dict(), ensure_ascii=False))
    assert payload["accepted"] == ["2026-09-16-7_8-1"]
    assert payload["notified"][0]["kind"] == KIND_ACCEPTED
    assert payload["seen_docs"] == 1


# ---------------------------------------------------------------- 删除误报防护
def test_hidden_from_listing_is_not_a_deletion(tmp_path: Path) -> None:
    """列表里看不到、但单独读还能读到 → 绝不能判成删除（分页抖动/权限抖动）。"""
    h = build_harness(tmp_path, [make_doc(1)], delete_grace_rounds=1)
    h.run()
    h.box.ack_all()
    h.source.hide(1)
    report = h.run()
    assert report.deleted == [] and h.notices() == []
    assert "消失但读取未确认（再等一轮）" in dict(report.skipped)
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED


def test_empty_doc_listing_is_not_a_snapshot(tmp_path: Path) -> None:
    """『目录非空但文档列表为空』几乎一定是接口异常，不能拿去判删除。"""
    items = [toc_title("week", "0914-0920"), toc_doc("n1", 1, "新生见面会", parent="week")]
    h = build_harness(tmp_path, [make_doc(1)], items=items, delete_grace_rounds=1)
    h.run()
    h.source.empty_listing = True
    report = h.run()
    assert report.snapshot_ok is False and report.deleted == []
    assert report.errors and "快照不可信" in report.errors[0]
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED


def test_index_keeps_deleted_submitted_application(tmp_path: Path) -> None:
    """源文档被删了，申请文件与索引都还要在（消费方不能漏单）。"""
    h = build_harness(tmp_path, [make_doc(1)], delete_grace_rounds=1)
    h.run()
    assert len(h.index()["applications"]) == 1

    h.source.remove(1)
    h.run()
    assert h.state_file()["docs"]["1"]["deleted_at"]  # 变成墓碑
    assert h.notices(KIND_DELETED_SUBMITTED)  # 也通知了
    assert len(h.applications()) == 1  # 申请文件还在
    index = h.index()
    assert [row["application_id"] for row in index["applications"]] == ["2026-09-16-7_8-1"]
    assert index["applications"][0]["activity"]["period"] == "7-8"


def test_index_is_rebuilt_from_files_not_only_state(tmp_path: Path) -> None:
    """索引每轮从 applications/*.json 重建：手写的申请文件也会进索引。"""
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    extra = h.outdir / "applications" / "2026-09-17-1_2-999.json"
    extra.write_text(
        json.dumps(
            {
                "application_id": "2026-09-17-1_2-999",
                "packaged_at": "2026-09-12T09:00:00+08:00",
                "source": {"doc_id": 999, "title": "手工补的"},
                "activity": {"date": "2026-09-17", "period": "1-2"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    h.run()
    ids = [row["application_id"] for row in h.index()["applications"]]
    assert ids == ["2026-09-16-7_8-1", "2026-09-17-1_2-999"]


def test_unmounted_legacy_approval_log_is_skipped(tmp_path: Path) -> None:
    """没挂进目录的旧版「审批日志」也不能被当成申请去打扰作者。"""
    h = build_harness(tmp_path, [make_doc(1), make_doc(2, "审批日志", "旧版遗留")])
    report = h.run()
    assert dict(report.skipped) == {"审批日志（旧版遗留）": 1}
    assert h.state_file()["docs"].get("2") is None


# ---------------------------------------------------------------- 压测暴露的两个场景
def test_webhook_delete_is_ignored_when_doc_still_exists(tmp_path: Path) -> None:
    """webhook 说「删了」，但文档其实还在（动作映射不准）→ 不能发错消息、不能丢记录。

    这正是第一次端到端压测抓到的 bug：误信删除事件 → 丢记录 → 下一轮把同一篇
    又受理一遍，社员收到两条「已受理」。
    """
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    h.box.ack_all()
    assert h.pipeline.handle_delete(1) is None  # 读得到 → 不认这个删除事件
    assert h.notices() == []  # 不发「不允许撤回」
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED
    h.run()
    assert h.notices() == []  # 也不会被重新受理


def test_restored_submitted_doc_is_not_accepted_twice(tmp_path: Path) -> None:
    """文档被删（真删）后又恢复（语雀回收站）→ 不能重复受理、重复通知。"""
    h = build_harness(tmp_path, [make_doc(1)])
    h.run()
    accepted_before = len(h.notices(KIND_ACCEPTED))
    assert accepted_before == 1

    # 真删除 → 通知 + 墓碑
    doc = h.source.by_id[1]
    h.source.remove(1)
    h.run()
    h.run()
    assert h.notices(KIND_DELETED_SUBMITTED)
    assert h.state_file()["docs"]["1"]["deleted_at"]

    # 又回来了（同一个 doc_id、内容没变）
    h.source.set(doc)
    report = h.run()
    assert any("又出现了" in n for n in report.notes)
    assert len(h.notices(KIND_ACCEPTED)) == 1  # 没有第二条「已受理」
    assert not h.notices(KIND_DELETED_SUBMITTED)[1:]  # 也没有重复的删除通知
    assert h.state_file()["docs"]["1"]["status"] == store_mod.STATUS_SUBMITTED
    assert h.state_file()["docs"]["1"]["deleted_at"] == ""


def test_restored_rejected_doc_is_reviewed_again(tmp_path: Path) -> None:
    """被退回的文档删掉又恢复，且内容改好了 → 应该重新受理。"""
    h = build_harness(tmp_path, [make_doc(1, text=body(校区="火星"))])
    h.run()
    h.source.remove(1)
    h.run()
    h.run()
    assert h.state_file()["docs"]["1"]["deleted_at"]

    h.source.set(make_doc(1, text=body(校区="仙林"), updated_at="2026-09-12T12:00:00+08:00"))
    report = h.run()
    assert report.accepted == ["2026-09-16-7_8-1"]
