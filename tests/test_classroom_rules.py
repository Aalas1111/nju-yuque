"""教室申请「确定要素」规则层的单测（纯离线、确定性）。"""

from __future__ import annotations

from datetime import datetime

from nju_yuque.classroom import rules

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=rules.CN)  # 2026-09-12（周六）09:00


def make(**over: str) -> dict[str, str]:
    base = {
        "申请人": "张三",
        "活动日期": "2026-09-16",
        "活动时间": "16:10-18:00",
        "校区": "仙林",
        "教学楼": "",
        "教室": "",
        "人数": "",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 草稿标签
def test_draft_marker_reads_only_the_head() -> None:
    assert rules.draft_marker("【草稿】填完删掉本行\n\n申请人：张三")
    assert rules.draft_marker("[草稿]")
    assert rules.draft_marker("# 草稿")
    assert rules.draft_marker("状态：草稿")  # 兼容上一版模板的写法
    # 只看开头 3 个非空行：正文里偶然提到「草稿」不算
    assert rules.draft_marker("申请人：张三\n活动日期：x\n活动时间：y\n这是草稿阶段的想法") is None
    # 空行不占窗口：下面这句里「草稿」是第 2 个非空行，仍然算草稿
    assert rules.draft_marker("申请人：张三\n\n\n\n草稿")
    assert rules.draft_marker("") is None
    assert rules.is_draft("\n\n   \n【草稿】xxx") is True


# ---------------------------------------------------------------- 字段解析
def test_parse_fields_tolerates_markup() -> None:
    body = (
        "# 申请\n\n"
        "- **申请人**：李四\n"
        "> 活动日期: 2026年9月16日\n"
        "* 活动时间：下午4点-6点\n"
        "校区：（必填）\n"
        "人数：\n"
    )
    fields = rules.parse_fields(body)
    assert fields["申请人"] == "李四"
    assert fields["活动日期"] == "2026年9月16日"
    assert fields["活动时间"] == "下午4点-6点"
    assert fields["校区"] == ""
    # 空值字段不能把下一行吞掉
    assert "人数" in fields and fields["人数"] == ""


def test_parse_fields_keeps_first_value() -> None:
    fields = rules.parse_fields("申请人：张三\n申请人：李四\n")
    assert fields["申请人"] == "张三"


# ---------------------------------------------------------------- 规范化
def test_normalize_date() -> None:
    assert rules.normalize_date("2026-09-16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert rules.normalize_date("9月16日", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert rules.normalize_date("2026/9/16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert rules.normalize_date("09-16", NOW)[0].strftime("%Y-%m-%d") == "2026-09-16"
    assert rules.normalize_date("下周", NOW)[0] is None
    assert rules.normalize_date("01-05", NOW)[0].year == 2027  # 只写月日且已过去很久 → 明年
    assert "→" in rules.normalize_date("9月16日", NOW)[1]


def test_normalize_time_standard() -> None:
    assert rules.normalize_time("16:10-18:00").start == 970
    assert rules.normalize_time("16:10-18:00").end == 1080
    assert rules.normalize_time("16:10-18:00").fix == ""
    assert rules.normalize_time("下午4点-6点").start == 960
    assert rules.normalize_time("晚上7点-9点").start == 1140
    assert rules.normalize_time("8:30~9:30").start == 510
    assert rules.normalize_time("随便").start is None


def test_normalize_time_marks_twelve_hour_ambiguity() -> None:
    """12 小时制不写上下午 → 放行但必须打 WARNING（用户分析里明确要求）。"""
    morning = rules.normalize_time("八点到九点")
    assert (morning.start, morning.end) == (480, 540)  # 中文数字也认
    assert morning.warnings, "八点到九点 必须提醒歧义"

    # 「两点到三点」→ 凌晨不可能借教室，按下午理解，同时提醒
    night = rules.normalize_time("两点到三点")
    assert (night.start, night.end) == (840, 900)
    assert night.warnings

    # 写了上下午就没有歧义提示
    assert rules.normalize_time("下午两点到三点").warnings == ()
    assert rules.normalize_time("上午八点到九点").warnings == ()
    assert rules.normalize_time("晚上七点到九点").start == 1140
    assert rules.normalize_time("十二点到十三点").start == 720


def test_cn_numeral_to_int() -> None:
    assert rules.cn_numeral_to_int("八") == 8
    assert rules.cn_numeral_to_int("十二") == 12
    assert rules.cn_numeral_to_int("二十") == 20
    assert rules.cn_numeral_to_int("二十三") == 23
    assert rules.cn_numeral_to_int("16") == 16
    assert rules.cn_numeral_to_int("两") == 2
    assert rules.cn_numeral_to_int("") is None
    assert rules.cn_numeral_to_int("很多") is None


def test_normalize_campus_and_people() -> None:
    assert rules.normalize_campus("仙林")[:2] == ("仙林", "3")
    assert rules.normalize_campus("仙林校区")[:2] == ("仙林", "3")
    assert rules.normalize_campus("南京大学苏州校区")[:2] == ("苏州", "4")
    assert rules.normalize_campus("火星")[:2] == ("", "")
    assert rules.normalize_people("30人")[:2] == (30, "人数「30人」→ 30")
    assert rules.normalize_people("")[:2] == (None, "")
    assert rules.normalize_people("很多")[2]  # 第 3 个返回值是问题描述
    assert rules.normalize_people("0")[2]


# ---------------------------------------------------------------- 节次推算
def test_derive_periods_user_examples() -> None:
    assert rules.derive_periods(8 * 60 + 30, 9 * 60 + 30) == (1, 2)
    assert rules.derive_periods(8 * 60 + 30, 9 * 60) == (1, 1)
    assert rules.derive_periods(16 * 60 + 10, 18 * 60) == (7, 8)
    assert rules.derive_periods(8 * 60, 12 * 60) == (1, 4)
    assert rules.derive_periods(14 * 60, 21 * 60 + 20) == (5, 11)
    assert rules.derive_periods(16 * 60, 17 * 60) == (7, 7)
    assert rules.derive_periods(9 * 60 + 50, 10 * 60 + 10) == (2, 3)
    assert rules.derive_periods(12 * 60 + 30, 13 * 60 + 30) is None
    assert rules.period_span((7, 8)) == "7-8"
    assert rules.period_span((5, 5)) == "5"


def test_period_table_matches_school_timetable() -> None:
    table = dict((p, (s, e)) for p, s, e in rules.period_table())
    assert table[1] == ("08:00", "08:50")
    assert table[7] == ("16:10", "17:00")
    assert table[12] == ("21:30", "22:20")
    assert len(table) == 12


def test_subtract_lunch() -> None:
    assert rules.subtract_lunch(16 * 60, 18 * 60) == [(960, 1080)]
    assert rules.subtract_lunch(12 * 60 + 30, 13 * 60 + 30) == []
    assert rules.subtract_lunch(13 * 60, 15 * 60) == [(840, 900)]
    assert rules.subtract_lunch(11 * 60, 15 * 60) == [(660, 720), (840, 900)]


# ---------------------------------------------------------------- 标题
def test_title_problem() -> None:
    assert rules.title_problem("") is not None
    assert rules.title_problem("无标题文档") is not None
    assert rules.title_problem("指导文档（必读）") is not None
    assert rules.title_problem("审批日志") is not None
    assert rules.title_problem("教室申请模板") is not None
    assert rules.title_problem("A" * 61) is not None
    for title in ("新生见面会", "思维训练营第 3 期", "读书会"):
        assert rules.title_problem(title) is None


# ---------------------------------------------------------------- 四档判定
def test_ok_tier() -> None:
    v = rules.evaluate(make(), title="新生见面会", author="张三", now=NOW)
    assert v.ok and v.tier == "ok"
    assert v.activity is not None
    assert v.activity.period == "7-8"
    assert (v.activity.period_start, v.activity.period_end) == (7, 8)
    assert v.activity.campus_code == "3"
    assert v.activity.people == rules.DEFAULT_PEOPLE
    assert v.activity.people_source == "default"
    assert v.activity.borrow_type == "团学活动"
    assert v.warnings == []


def test_normalized_tier() -> None:
    v = rules.evaluate(
        make(活动日期="9月16日", 活动时间="下午4点-6点", 校区="仙林校区"),
        title="新生见面会",
        author="张三",
        now=NOW,
    )
    assert v.ok and v.tier == "normalized"
    assert v.activity is not None
    assert (v.activity.date, v.activity.start, v.activity.end) == ("2026-09-16", "16:00", "18:00")
    assert any("日期" in f for f in v.fixes)
    assert any("校区" in f for f in v.fixes)
    assert any("时间" in f for f in v.fixes)


def test_people_from_document() -> None:
    v = rules.evaluate(make(人数="50人"), title="t", author="a", now=NOW)
    assert v.ok and v.activity is not None
    assert v.activity.people == 50 and v.activity.people_source == "document"
    assert any("人数" in f for f in v.fixes)


def test_people_too_many_is_only_a_warning() -> None:
    v = rules.evaluate(make(人数="900"), title="t", author="a", now=NOW)
    assert v.ok and v.activity is not None and v.activity.people == 900
    assert any("人数" in w for w in v.warnings)


def test_applicant_can_be_inferred_from_creator() -> None:
    v = rules.evaluate(make(申请人=""), title="社团分享会", author="李四", now=NOW)
    assert v.ok and v.activity is not None
    assert v.fields["申请人"] == "李四"
    assert any("申请人" in f for f in v.fixes)


def test_warning_long_span() -> None:
    v = rules.evaluate(make(活动时间="16:00-22:00"), title="t", author="a", now=NOW)
    assert v.ok and v.activity is not None
    assert any("小时" in w for w in v.warnings)


def test_lunch_handling() -> None:
    # 完全落在午饭里 → 退回（这段不需要借教室）
    v = rules.evaluate(make(活动时间="12:30-13:30"), title="t", author="a", now=NOW)
    assert not v.ok and any("不需要借教室" in p for p in v.problems)
    # 只跨一边 → 按可用时段推节次
    v2 = rules.evaluate(make(活动时间="13:00-15:00"), title="t", author="a", now=NOW)
    assert v2.ok and v2.activity is not None and v2.activity.period == "5"
    assert any("吃饭时间" in f for f in v2.fixes)
    # 两边都跨 → 一次申请覆盖第 4-5 节
    v3 = rules.evaluate(make(活动时间="11:00-15:00"), title="t", author="a", now=NOW)
    assert v3.ok and v3.activity is not None
    assert v3.activity.period == "4-5"


def test_rejected_cases() -> None:
    too_soon = rules.evaluate(
        make(活动日期="2026-09-14", 活动时间="08:00-09:00"), title="t", author="a", now=NOW
    )
    assert not too_soon.ok and any("不足 48 小时" in p for p in too_soon.problems)
    assert too_soon.activity is None

    past = rules.evaluate(
        make(活动日期="2026-09-10", 活动时间="08:00-09:00"), title="t", author="a", now=NOW
    )
    assert not past.ok and any("已经过去" in p for p in past.problems)

    out_of_window = rules.evaluate(make(活动时间="23:00-23:30"), title="t", author="a", now=NOW)
    assert not out_of_window.ok and any("超出可申请时段" in p for p in out_of_window.problems)

    reversed_time = rules.evaluate(make(活动时间="17:00-16:00"), title="t", author="a", now=NOW)
    assert not reversed_time.ok and any("颠倒" in p for p in reversed_time.problems)

    bad_campus = rules.evaluate(make(校区="火星"), title="t", author="a", now=NOW)
    assert not bad_campus.ok and any("不认识" in p for p in bad_campus.problems)

    bad_date = rules.evaluate(make(活动日期="下周"), title="t", author="a", now=NOW)
    assert not bad_date.ok and any("无法解析" in p for p in bad_date.problems)

    missing = rules.evaluate(
        make(申请人="", 活动日期="", 活动时间="", 校区=""), title="t", author="", now=NOW
    )
    assert not missing.ok and len(missing.problems) >= 4

    blank_title = rules.evaluate(make(), title="无标题文档", author="a", now=NOW)
    assert not blank_title.ok and any("标题" in p for p in blank_title.problems)


def test_fingerprint_is_stable_and_reason_sensitive() -> None:
    a = rules.evaluate(make(校区="火星"), title="t", author="a", now=NOW)
    b = rules.evaluate(make(校区="火星"), title="t", author="a", now=NOW)
    c = rules.evaluate(make(校区="火星", 人数="0"), title="t", author="a", now=NOW)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


def test_far_future_date_is_warning_not_rejection() -> None:
    v = rules.evaluate(make(活动日期="2026-11-16"), title="t", author="a", now=NOW)
    assert v.ok and v.activity is not None
    assert any("超过" in w for w in v.warnings)


# ---------------------------------------------------------------- 抢救未用模板的文档
def test_salvage_freeform_doc() -> None:
    text = "下周三 9月16日 下午 16:00-18:00 在仙林借个教室，社团分享会"
    v = rules.salvage(text, title="社团分享会", author="王五", now=NOW)
    assert v is not None and v.ok and v.activity is not None
    assert v.activity.period == "7-8"
    assert any("未使用模板" in f for f in v.fixes)


def test_salvage_returns_none_when_hopeless() -> None:
    assert rules.salvage("今天天气不错，随便写点东西。", title="随笔", author="a", now=NOW) is None


def test_unrecognized_helper() -> None:
    v = rules.unrecognized("看不出是申请")
    assert v.tier == "unrecognized" and not v.ok and v.activity is None
    assert v.fingerprint


def test_skip_helper() -> None:
    v = rules.skip("指导文档（结构性）")
    assert v.tier == "skip" and "指导文档" in v.skip_reason


# ---------------------------------------------------------------- 去重指纹不能用「会变的文案」
def test_fingerprint_ignores_time_dependent_text() -> None:
    """「距现在仅 47.0 小时」下一轮变成 46.5 小时，指纹必须不变（否则每轮都重新通知）。"""
    later = datetime(2026, 9, 12, 9, 30, tzinfo=rules.CN)
    a = rules.evaluate(
        make(活动日期="2026-09-14", 活动时间="08:00-09:00"), title="t", author="a", now=NOW
    )
    b = rules.evaluate(
        make(活动日期="2026-09-14", 活动时间="08:00-09:00"), title="t", author="a", now=later
    )
    assert not a.ok and not b.ok
    assert a.problems != b.problems  # 文案确实变了（小时数不同）
    assert a.fingerprint == b.fingerprint  # 但「问题」没变
    assert a.problem_codes == b.problem_codes == ["too_soon"]


def test_fingerprint_changes_when_problem_category_changes() -> None:
    bad_campus = rules.evaluate(make(校区="火星"), title="t", author="a", now=NOW)
    missing = rules.evaluate(make(校区=""), title="t", author="a", now=NOW)
    assert bad_campus.fingerprint != missing.fingerprint
    assert bad_campus.problem_codes == ["bad_campus"]
    assert missing.problem_codes == ["missing_field:校区"]


def test_problem_codes_cover_all_rejection_kinds() -> None:
    cases = {
        "bad_title": dict(title="", **{}),
        "bad_date": {"活动日期": "下周"},
        "bad_time": {"活动时间": "随便"},
        "time_reversed": {"活动时间": "17:00-16:00"},
        "out_of_window": {"活动时间": "23:00-23:30"},
        "lunch_only": {"活动时间": "12:30-13:30"},
        "bad_campus": {"校区": "火星"},
        "bad_people": {"人数": "很多"},
        # 18:00-18:30 落在「第 8 节结束（17:10-18:00）」与「第 9 节开始（18:30）」
        # 之间的空档里，对不上任何节次
        "period_mismatch": {"活动时间": "18:00-18:25"},
    }
    for code, over in cases.items():
        title = over.pop("title", "t")
        v = rules.evaluate(make(**over), title=title, author="a", now=NOW)
        assert code in v.problem_codes, (code, v.problem_codes, v.problems)


def test_salvage_keeps_leading_hint() -> None:
    """免模板的长句里「晚上8点到9点」必须是 20:00，不能按早上 8 点算。"""
    text = "下周三 9月16日 晚上8点到9点 在仙林借个教室，社团分享会"
    v = rules.salvage(text, title="社团分享会", author="王五", now=NOW)
    assert v is not None and v.activity is not None
    assert v.activity.start == "20:00" and v.activity.end == "21:00"
    # 晚上的档位是 18:30 / 19:30 / 20:30…，所以 20:00-21:00 会覆盖第 10-11 节
    # （agent 宁可多借一点，也不让活动办到一半被赶出来）
    assert v.activity.period == "10-11"

    morning = rules.salvage(
        "下周三 9月16日 上午8点到9点 在仙林借个教室", title="早读", author="王五", now=NOW
    )
    assert morning is not None and morning.activity is not None
    assert morning.activity.start == "08:00"

    # 提示词和时间之间有空格也要认（否则「晚上 8点」会被当成早上 8 点）
    spaced = rules.salvage(
        "下周三 9月16日 晚上 8点到9点 在仙林借个教室", title="夜读", author="王五", now=NOW
    )
    assert spaced is not None and spaced.activity is not None
    assert spaced.activity.start == "20:00"
