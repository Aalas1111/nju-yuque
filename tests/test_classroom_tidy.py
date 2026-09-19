"""周的计算 + 目录整理（tidy）规划器单测——纯离线，不联网。"""

from __future__ import annotations

from datetime import date

from nju_yuque.classroom import toc as toc_mod
from nju_yuque.classroom import week as week_mod
from nju_yuque.models import TocItem

TODAY = date(2026, 9, 19)  # 周六，属于 0914-0920


def toc_item(uuid: str, type_: str, title: str, parent: str = "", doc_id: int | None = None):
    return TocItem(
        uuid=uuid,
        type=type_,
        title=title,
        url="",
        slug="",
        doc_id=doc_id,
        level=None,
        parent_uuid=parent,
        child_uuid="",
    )


# ---------------------------------------------------------------- 周计算
def test_week_of_and_title() -> None:
    wk = week_mod.week_of(TODAY)
    assert (wk.start, wk.end) == (date(2026, 9, 14), date(2026, 9, 20))
    assert wk.title == "0914-0920"
    assert wk.contains(date(2026, 9, 14)) and wk.contains(date(2026, 9, 20))
    assert not wk.contains(date(2026, 9, 21))
    assert week_mod.week_of(date(2026, 9, 21)).title == "0921-0927"
    assert week_mod.next_week(TODAY).title == "0921-0927"
    # 周一也是一周的开始
    assert week_mod.week_of(date(2026, 9, 21)).start == date(2026, 9, 21)
    # 周日属于本周，不属于下一周
    assert week_mod.week_of(date(2026, 9, 20)).title == "0914-0920"


def test_parse_week_title() -> None:
    wk = week_mod.parse_week_title("0921-0927", today=TODAY)
    assert wk is not None and (wk.start, wk.end) == (date(2026, 9, 21), date(2026, 9, 27))
    assert week_mod.parse_week_title("0914~0920", today=TODAY) is not None
    assert week_mod.parse_week_title("归档区", today=TODAY) is None
    assert week_mod.parse_week_title("", today=TODAY) is None
    assert week_mod.is_week_title("0921-0927") and not week_mod.is_week_title("归档区")


def test_parse_week_title_across_new_year() -> None:
    """跨年的目录名（1229-0104）要往后一年解释，不能变成 1 月的过去。"""
    wk = week_mod.parse_week_title("1229-0104", today=date(2026, 12, 30))
    assert wk is not None
    assert wk.start == date(2026, 12, 29) and wk.end == date(2027, 1, 4)
    wk2 = week_mod.parse_week_title("1229-0104", today=date(2027, 1, 2))
    assert wk2 is not None and wk2.end == date(2027, 1, 4)


# ---------------------------------------------------------------- 规划器
def sample_toc() -> list[TocItem]:
    """模拟真机现状：指导文档 / 0914-0920 / 归档区(0907-0913) / 0921-0927。"""
    return [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("w0907", "TITLE", "0907-0913", parent="arch"),
        toc_item("w0921", "TITLE", "0921-0927"),
    ]


def test_plan_archives_stale_folders_and_pins_archive_last() -> None:
    plan = toc_mod.plan_tidy(sample_toc(), today=TODAY)
    kinds = [op.kind for op in plan.ops]
    assert plan.active_title == "0914-0920"
    # 今天在 0914-0920 这一周 → 它是活跃目录；下周的 0921-0927 被归档（它不在本周）
    assert plan.archived == ["0921-0927"]
    assert toc_mod.OP_ARCHIVE in kinds
    assert toc_mod.OP_ARCHIVE_LAST in kinds
    archived = [op.title for op in plan.ops if op.kind == toc_mod.OP_ARCHIVE]
    assert archived == ["0921-0927"]
    archive_last = next(op for op in plan.ops if op.kind == toc_mod.OP_ARCHIVE_LAST)
    assert archive_last.node_uuid == "arch"


def test_plan_is_idempotent() -> None:
    """已经是目标形态 → 不产生任何操作（常驻每轮跑也不会瞎折腾）。"""
    items = [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("w0907", "TITLE", "0907-0913", parent="arch"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    assert plan.empty, plan.describe()


def test_plan_creates_active_folder_when_missing() -> None:
    items = [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("w0907", "TITLE", "0907-0913", parent="arch"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    assert any(op.kind == toc_mod.OP_CREATE and op.title == "0914-0920" for op in plan.ops)
    assert any("将新建" in n for n in plan.notes)


def test_plan_orders_archive_newest_first() -> None:
    items = [
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("a0907", "TITLE", "0907-0913", parent="arch"),
        toc_item("a0831", "TITLE", "0831-0906", parent="arch"),
        toc_item("a0921", "TITLE", "0921-0927", parent="arch"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    order_op = next((op for op in plan.ops if op.kind == toc_mod.OP_ORDER_ARCHIVE), None)
    assert order_op is not None, plan.describe()
    # 0914-0920 是活跃目录（在根目录），归档区里只有这三个 → 按「新→旧」排
    assert order_op.note == "0921-0927→0907-0913→0831-0906"


def test_plan_does_not_touch_non_week_folders() -> None:
    items = [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("notes", "TITLE", "会议记录"),
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    assert all(op.title != "会议记录" for op in plan.ops)


def test_plan_warns_about_unsettled_applications() -> None:
    items = sample_toc()
    plan = toc_mod.plan_tidy(items, today=TODAY, unsettled={"0921-0927": 3})
    assert any("3 份已受理但未结案" in w for w in plan.warnings)
    # 只是告警，不阻止归档
    assert "0921-0927" in plan.archived


def test_plan_bails_out_on_duplicate_folders() -> None:
    items = [
        toc_item("a1", "TITLE", "归档区"),
        toc_item("a2", "TITLE", "归档区"),
        toc_item("w0914", "TITLE", "0914-0920"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    assert plan.ops == [] and any("重名" in w for w in plan.warnings)


def test_plan_notes_when_archive_missing() -> None:
    items = [toc_item("w0914", "TITLE", "0914-0920")]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    assert any("还没有「归档区」" in n for n in plan.notes)


def test_apply_tidy_calls_the_reliable_primitives() -> None:
    """执行器只应使用 appendNode（带/不带 target），不能用 editNode+prev_uuid。"""

    class FakeApi:
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self.items = sample_toc()

        def toc_add(self, repo: str, **kw: object) -> list:
            self.calls.append(("toc_add", repo, kw.get("title")))
            return []

        def toc(self, repo: str) -> list:
            return self.items

        def toc_place(self, repo: str, **kw: object) -> list:
            self.calls.append(("toc_place", kw.get("node_uuid"), kw.get("target_uuid", "")))
            return []

        def toc_edit(self, *a: object, **kw: object) -> list:  # pragma: no cover
            raise AssertionError("不该用 toc_edit（editNode + prev_uuid 会静默失败）")

    api = FakeApi()
    plan = toc_mod.plan_tidy(sample_toc(), today=TODAY)
    done = toc_mod.apply_tidy(api, "g/r", plan, log=lambda _t: None)
    assert done == [op.describe() for op in plan.ops]
    places = [c for c in api.calls if c[0] == "toc_place"]
    assert ("toc_place", "arch", "") in places  # 归档区置底 = 不带 target 的 appendNode
    assert any(c[1] == "w0921" and c[2] == "arch" for c in places)  # 挪进归档区


def test_plan_unarchives_the_folder_that_became_active() -> None:
    """上周建好的「下周目录」到点了要能从归档区搬回来，而不是重复再建一个。"""
    items = [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("a0921", "TITLE", "0921-0927", parent="arch"),
        toc_item("a0907", "TITLE", "0907-0913", parent="arch"),
    ]
    # 下周一（0921）
    plan = toc_mod.plan_tidy(items, today=date(2026, 9, 21))
    assert plan.active_title == "0921-0927"
    assert [op.kind for op in plan.ops].count(toc_mod.OP_CREATE) == 0, plan.describe()
    assert any(op.kind == toc_mod.OP_UNARCHIVE and op.node_uuid == "a0921" for op in plan.ops), (
        plan.describe()
    )
    assert "0914-0920" in plan.archived  # 上周的归档掉
    assert toc_mod.OP_ARCHIVE_LAST in [op.kind for op in plan.ops]


def test_plan_reorders_taking_incoming_into_account() -> None:
    """本轮要归档进来的目录也要参与排序，否则刚归档的会沉到最下面。"""
    items = [
        toc_item("guide", "DOC", "指导文档（必读）", doc_id=1),
        toc_item("w0914", "TITLE", "0914-0920"),
        toc_item("arch", "TITLE", "归档区"),
        toc_item("a0907", "TITLE", "0907-0913", parent="arch"),
        toc_item("w0921", "TITLE", "0921-0927"),
    ]
    plan = toc_mod.plan_tidy(items, today=TODAY)
    order_op = next((op for op in plan.ops if op.kind == toc_mod.OP_ORDER_ARCHIVE), None)
    assert order_op is not None, plan.describe()
    # 0921-0927 是新的，必须排到 0907-0913 前面
    assert order_op.note == "0921-0927→0907-0913"
