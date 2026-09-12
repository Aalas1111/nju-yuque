"""教室申请 agent 的判定逻辑测试（离线、确定性）。

被测模块在 examples/ 下（是参考实现不是打包代码），用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "classroom_agent", ROOT / "examples" / "classroom_application_agent.py"
)
assert _spec and _spec.loader
agent = importlib.util.module_from_spec(_spec)
sys.modules["classroom_agent"] = agent  # dataclass 需要能在 sys.modules 里找到本模块
_spec.loader.exec_module(agent)

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=agent.CN)  # 假设现在是 09-12 09:00


def make(**over: str) -> dict[str, str]:
    base = {
        "状态": "待提交",
        "活动名称": "新生见面会",
        "申请人": "张三",
        "活动日期": "2026-09-16",
        "活动时间": "16:10-18:00",
        "校区": "仙林",
        "教学楼": "",
        "教室": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 节次推算
def test_derive_periods_user_examples() -> None:
    # 用户给的例子：8:30-9:30 占第 1、2 节；8:30-9:00 只占第 1 节
    assert agent.derive_periods(8 * 60 + 30, 9 * 60 + 30) == (1, 2)
    assert agent.derive_periods(8 * 60 + 30, 9 * 60) == (1, 1)


def test_derive_periods_more() -> None:
    assert agent.derive_periods(16 * 60 + 10, 18 * 60) == (7, 8)
    assert agent.derive_periods(8 * 60, 8 * 60 + 50) == (1, 1)
    assert agent.derive_periods(8 * 60, 12 * 60) == (1, 4)
    assert agent.derive_periods(14 * 60, 21 * 60 + 20) == (5, 11)
    # 「一小时一档」：第7节 = 16:00-17:00，课间 10 分钟不计
    assert agent.derive_periods(16 * 60, 17 * 60) == (7, 7)
    assert agent.derive_periods(17 * 60 + 30, 18 * 60) == (8, 8)
    assert agent.derive_periods(9 * 60 + 50, 10 * 60 + 10) == (2, 3)
    # 不落在任何档位里
    assert agent.derive_periods(12 * 60 + 30, 13 * 60 + 30) is None
    assert agent.derive_periods(3 * 60, 4 * 60) is None


# ---------------------------------------------------------------- 规范化
def test_normalize_date() -> None:
    assert agent.normalize_date("2026-09-16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert agent.normalize_date("9月16日", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert agent.normalize_date("2026/9/16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert agent.normalize_date("09-16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert agent.normalize_date("下周", NOW)[0] is None
    # 只写月日且已过去很久 → 视作明年
    assert agent.normalize_date("01-05", NOW)[0].year == 2027


def test_normalize_time() -> None:
    assert agent.normalize_time("16:10-18:00")[:2] == (970, 1080)
    assert agent.normalize_time("下午4点-6点")[:2] == (960, 1080)
    assert agent.normalize_time("晚上7点-9点")[:2] == (1140, 1260)
    assert agent.normalize_time("8:30~9:30")[:2] == (510, 570)
    assert agent.normalize_time("4点半-6点")[:2] == (990, 1080)  # 无提示的小时按下午理解
    assert agent.normalize_time("随便")[:2] == (None, None)
    # 非标准写法要给出规范化说明
    assert agent.normalize_time("下午4点-6点")[2]


def test_normalize_campus() -> None:
    assert agent.normalize_campus("仙林")[:2] == ("仙林", "3")
    assert agent.normalize_campus("仙林校区")[:2] == ("仙林", "3")
    assert agent.normalize_campus("南京大学苏州校区")[:2] == ("苏州", "4")
    assert agent.normalize_campus("火星")[:2] == ("", "")


# ---------------------------------------------------------------- 三档判断
def test_tier_ok() -> None:
    v = agent.evaluate(make(), title="新生见面会", author="张三", now=NOW)
    assert v.ok and v.tier == "ok"
    assert v.new_status == agent.STATUS_SUBMITTED
    assert v.derived["借用节次"] == "7-8"
    assert v.derived["XXXQDM"] == "3"
    assert v.derived["人数"] == agent.DEFAULT_PEOPLE
    assert v.derived["联系电话"] == agent.DEFAULT_CONTACT


def test_tier_normalized() -> None:
    v = agent.evaluate(
        make(活动日期="9月16日", 活动时间="下午4点-6点", 校区="仙林校区"),
        title="新生见面会",
        author="张三",
        now=NOW,
    )
    assert v.ok and v.tier == "normalized"
    assert v.derived["日期"] == "2026-09-16"
    assert v.derived["开始"] == "16:00"
    assert v.derived["借用节次"] == "7-8"
    assert any("日期" in f for f in v.fixes)
    assert any("校区" in f for f in v.fixes)


def test_time_not_aligned_is_still_ok() -> None:
    """节次起点本来就不规则（16:10/18:30），所以不做整点对齐。"""
    v = agent.evaluate(make(活动时间="16:05-18:05"), title="t", author="张三", now=NOW)
    assert v.ok
    assert v.derived["开始"] == "16:05" and v.derived["结束"] == "18:05"
    assert v.derived["借用节次"] == "7-8"


def test_optional_fields_can_be_empty() -> None:
    v = agent.evaluate(make(教学楼="", 教室=""), title="t", author="张三", now=NOW)
    assert v.ok
    assert v.derived["教学楼"] == "(随机)" and v.derived["教室"] == "(随机)"


def test_fallback_name_and_applicant() -> None:
    v = agent.evaluate(make(活动名称="", 申请人=""), title="社团分享会", author="李四", now=NOW)
    assert v.ok and v.tier == "normalized"
    assert v.fields["活动名称"] == "社团分享会"
    assert v.fields["申请人"] == "李四"


def test_rejected_too_soon() -> None:
    v = agent.evaluate(
        make(活动日期="2026-09-14", 活动时间="08:00-09:00"), title="t", author="a", now=NOW
    )
    assert not v.ok and v.tier == "rejected"
    assert v.new_status == agent.STATUS_REJECTED
    assert any("不足 48 小时" in p for p in v.problems)


def test_lunch_fully_inside_is_rejected() -> None:
    v = agent.evaluate(make(活动时间="12:30-13:30"), title="t", author="a", now=NOW)
    assert not v.ok
    assert any("不需要借教室" in p for p in v.problems)


def test_lunch_partial_overlap_is_clamped() -> None:
    """午饭时段不是「违规」，而是这段不需要借教室。"""
    # 13:00-15:00 → 只借 14:00-15:00
    v = agent.evaluate(make(活动时间="13:00-15:00"), title="t", author="a", now=NOW)
    assert v.ok and v.tier == "normalized"
    assert v.derived["开始"] == "14:00" and v.derived["结束"] == "15:00"
    assert v.derived["借用节次"] == "5"
    assert any("只借" in f for f in v.fixes)
    # 11:30-12:30 → 只借 11:30-12:00（第 4 节）
    v2 = agent.evaluate(make(活动时间="11:30-12:30"), title="t", author="a", now=NOW)
    assert v2.ok and v2.derived["借用节次"] == "4"
    # 正好 11:00-12:00 / 14:00-15:00 不用改
    assert agent.evaluate(make(活动时间="11:00-12:00"), title="t", author="a", now=NOW).tier == "ok"


def test_lunch_spanning_both_sides_is_rejected() -> None:
    v = agent.evaluate(make(活动时间="11:00-15:00"), title="t", author="a", now=NOW)
    assert not v.ok
    assert any("拆成上下午两场" in p for p in v.problems)


def test_rejected_out_of_window() -> None:
    v = agent.evaluate(make(活动时间="23:00-23:30"), title="t", author="a", now=NOW)
    assert not v.ok
    assert any("超出可申请时段" in p for p in v.problems)


def test_break_gap_counts_as_hour_block() -> None:
    """「一小时一档」下，课间 10 分钟也归在相邻档位里（不纠结）。"""
    v = agent.evaluate(make(活动时间="09:50-10:10"), title="t", author="a", now=NOW)
    assert v.ok and v.derived["借用节次"] == "2-3"
    # 而 12:30-13:30 在午饭时段，仍会被拒绝
    v2 = agent.evaluate(make(活动时间="12:30-13:30"), title="t", author="a", now=NOW)
    assert not v2.ok


def test_rejected_missing_and_bad_campus() -> None:
    v = agent.evaluate(make(校区=""), title="t", author="a", now=NOW)
    assert not v.ok and any("校区" in p for p in v.problems)
    v2 = agent.evaluate(make(校区="火星"), title="t", author="a", now=NOW)
    assert not v2.ok and any("不认识" in p for p in v2.problems)


def test_rejected_bad_date() -> None:
    v = agent.evaluate(make(活动日期="下周"), title="t", author="a", now=NOW)
    assert not v.ok and any("无法解析" in p for p in v.problems)


def test_skip_when_status_not_pending() -> None:
    for status in (
        "草稿",
        agent.STATUS_SUBMITTED,
        agent.STATUS_APPROVED,
        agent.STATUS_REJECTED,
        "",
    ):
        v = agent.evaluate(make(状态=status), title="t", author="a", now=NOW)
        assert v.tier == "skip" and not v.new_status


# ---------------------------------------------------------------- 抢救未用模板的文档
def test_salvage_freeform_doc() -> None:
    body = "下周三 9月16日 下午 16:00-18:00 在仙林借个教室，社团分享会"
    v = agent.salvage(body, title="社团分享会", author="王五", now=NOW)
    assert v is not None
    assert v.ok and v.tier == "normalized"
    assert v.fields["申请人"] == "王五"
    assert v.derived["借用节次"] == "7-8"
    assert any("未使用模板" in f for f in v.fixes)


def test_salvage_returns_none_when_hopeless() -> None:
    assert agent.salvage("今天天气不错，随便写点东西。", title="随笔", author="a", now=NOW) is None


# ---------------------------------------------------------------- 状态改写
def test_set_status_replaces_first_line() -> None:
    body = "状态：待提交\n\n活动名称：X\n"
    out = agent.set_status(body, agent.STATUS_REJECTED)
    assert out.startswith(f"状态：{agent.STATUS_REJECTED}")
    assert "活动名称：X" in out


def test_set_status_prepends_when_missing() -> None:
    out = agent.set_status("活动名称：X\n", agent.STATUS_SUBMITTED)
    assert out.startswith(f"状态：{agent.STATUS_SUBMITTED}")
    assert "活动名称：X" in out
