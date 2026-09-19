"""教室借用申请 · 「确定要素」规则层。

纯函数、离线可测、**不碰网络、不写语雀**——所有语雀交互都在
:mod:`nju_yuque.classroom.pipeline` / :mod:`nju_yuque.classroom.server` 里。

输入：一份申请文档的正文 + 标题 + 文档创建者 + 当前时间
输出：:class:`Verdict`

判定分四档（对应 CAC 说的「战略足够简单、战术足够细致」）：

===================  ================================================  ==========================
档位                  含义                                               pipeline 的动作
===================  ================================================  ==========================
``ok``                字段齐全、写法规范                                 打包 JSON + 通知「已受理」
``normalized``        写法不标准但能看懂 → agent 自动规范化              同上，JSON 里记 normalizations
``rejected``          缺信息 / 超期 / 超时段 / 时间对不上节次            通知「退回修改」
``unrecognized``      看不出是教室借用申请                              通知「无法识别」
===================  ================================================  ==========================

设计原则（沿用上一轮交接的结论）：

1. **能推断就推断，别让用户填表**：申请人可由文档创建者推断、人数给默认值、
   教学楼/教室不填就是随机、借用类型固定「团学活动」。
2. **可疑但能跑通的一律放行 + 打 WARNING**：时间过长、12 小时制歧义、
   日期离得远——决定权交回给人，而不是让社员来回改。
3. **算得出来的不让人填**：借用的「节次」由活动时间推算，社员只写几号几点几分。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .contract import Activity

CN = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 识别：草稿标签
# 唯一的状态来源：**文档开头有没有草稿标记**。有 → 永不处理；没有 → 当成申请处理。
# agent 不改文档、不写状态、不留审批日志，所以「状态」这件事只存在于本地持久化文件里。
DRAFT_HINTS = ("草稿",)
DRAFT_SCAN_LINES = 3  # 只看开头 3 个非空行，避免正文里偶然出现「草稿」就被永久跳过

# ---------------------------------------------------------------- 业务规则
ADVANCE_HOURS = 48  # 必须提前 48 小时（系统限制：借不到当日与次日）
DAY_START, DAY_END = 8 * 60, 22 * 60 + 20  # 可借用时段 08:00 - 22:20
LUNCH_START, LUNCH_END = 12 * 60, 14 * 60  # 吃饭时间：这段不需要借教室
SPAN_WARN_MINUTES = 3 * 60  # 活动时长 ≥ 3 小时 → WARNING（怕社员估时过于乐观）
EXPIRE_WARN_DAYS = 14  # 日期距今超过两周 → WARNING
PEOPLE_WARN_MAX = 500  # 人数超过这个数 → WARNING

# 真实上课时间（与学校课表一致），仅作对照与文档使用
PERIOD_STARTS = [480, 540, 610, 670, 840, 900, 970, 1030, 1110, 1170, 1230, 1290]
PERIOD_ENDS = [530, 590, 660, 720, 890, 950, 1020, 1080, 1160, 1220, 1280, 1340]

# 匹配用的「一小时一档」：档位起点 = 真实起点向下取到 30 分钟，档长 60 分钟。
# 于是 第 1 节 = 08:00-09:00、第 7 节 = 16:00-17:00（课间那 10 分钟不纠结），
# 晚上第 9 节 = 18:30-19:30（起点本来就在半点）。目的是容忍社员写「16:00-17:00」
# 这种不严格等于课表的时间，而不是要求他背课表。
PERIODS: list[tuple[int, int, int]] = [
    (i + 1, (s // 30) * 30, (s // 30) * 30 + 60) for i, s in enumerate(PERIOD_STARTS)
]

CAMPUSES: dict[str, tuple[str, str]] = {  # 别名 -> (规范名, 学校代码)
    "鼓楼": ("鼓楼", "1"),
    "浦口": ("浦口", "2"),
    "仙林": ("仙林", "3"),
    "苏州": ("苏州", "4"),
}

DEFAULT_PEOPLE = 30  # 人数不详时的默认值（小一点更容易借到）
DEFAULT_BORROW_TYPE = "团学活动"

MAX_TITLE_LEN = 60

# 文档标题 **就是活动名称**。以下是「没填标题」或「想冒充系统文档」的标题：
# 都当成不合规退回，而不是跳过（否则有人把申请命名成「指导文档」就能逃避审查）。
TITLE_BAD_EXACT = {"", "无标题文档", "无标题"}
TITLE_BAD_HINTS = ("指导文档", "填表说明", "申请模板", "使用说明", "审批日志", "归档区", "README")

REQUIRED_FIELDS = ("申请人", "活动日期", "活动时间", "校区")
OPTIONAL_FIELDS = ("教学楼", "教室", "人数")
ALL_FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)
TEMPLATE_MIN_FIELDS = 3  # 能认出「用了模板」的最少字段数（否则试着从正文里抢救）

FIELD_RE = re.compile(
    # 注意：[ \t] 而不是 \s —— \s 会吃掉换行，导致空值字段把下一行当成自己的值
    r"^[ \t>*\-•]*\**[ \t]*(" + "|".join(ALL_FIELDS) + r")[ \t]*\**[ \t]*[:：][ \t]*(.*?)[ \t]*$",
    re.MULTILINE,
)
DATE_HINT_RE = re.compile(
    r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*[-/.月]\s*\d{1,2})"
)
_TIME_TOKEN = r"\d{1,2}\s*[:：点时]\s*(?:\d{1,2}|半)?"  # 16:10 / 4点 / 4点半 / 16时20
TIME_SEP = r"(?:\s*(?:-|~|～|—|－|到|至|,|，|、|\s)\s*)"
TIME_HINT_RE = re.compile(
    rf"({_TIME_TOKEN})"
    rf"{TIME_SEP}"
    rf"(?:(?:上午|下午|晚上|早上|中午|傍晚)\s*)?({_TIME_TOKEN})"
)

# 社员经常写「八点到九点」「下午两点到三点」「九月二十三日」，所以得先认中文数字。
# 只处理紧跟在「点时月日号年」前面的那个数词，不动别的数字。
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CN_NUM_CHARS = "零一二三四五六七八九十两"
_CN_HOUR_RE = re.compile(rf"([{_CN_NUM_CHARS}]{{1,4}})\s*(?=[点时])")
_CN_DATE_RE = re.compile(rf"([{_CN_NUM_CHARS}]{{1,4}})\s*(?=[月日号年])")


# ---------------------------------------------------------------- 数据结构
@dataclass
class Verdict:
    """一次判定的结果。"""

    tier: str = ""  # ok / normalized / rejected / unrecognized / skip
    ok: bool = False
    problems: list[str] = field(default_factory=list)  # 退回原因（要发给社员）
    problem_codes: list[str] = field(default_factory=list)  # 稳定问题码（只用于去重指纹）
    warnings: list[str] = field(default_factory=list)  # 可疑但放行
    fixes: list[str] = field(default_factory=list)  # agent 自动规范化了什么
    fields: dict[str, str] = field(default_factory=dict)  # 文档里原样填写的字段
    activity: Activity | None = None  # 要素齐备时的结构化结果
    note: str = ""  # 内部说明（不发给社员）
    skip_reason: str = ""  # tier=skip 时的原因（结构性文档等）
    lunch_skipped: bool = False
    fingerprint: str = ""  # 「同样的问题不重复通知」用的指纹

    def sealed(self) -> Verdict:
        """算好「同样的问题不重复通知」用的指纹。

        指纹**只用稳定问题码**，不带任何会随时间变化的文案——否则
        「距现在仅 47.0 小时」下一轮变成 46.5 小时就会换指纹，同一份文档每轮都被重新通知。
        """
        if not self.fingerprint:
            if self.problem_codes:
                stable = "|".join([self.tier, *self.problem_codes])
            else:
                # 兜底：万一有调用方只填了文案没填码，也至少不能把所有问题塌缩成同一个指纹
                stable = "|".join([self.tier, *self.problems, self.skip_reason, self.note])
            self.fingerprint = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12]
        return self


def skip(reason: str) -> Verdict:
    """结构性跳过（指导文档 / 归档区 / 草稿 / 非普通文档）。"""
    return Verdict(tier="skip", skip_reason=reason, note=reason)


def unrecognized(note: str) -> Verdict:
    """看不出是申请。"""
    return Verdict(
        tier="unrecognized",
        ok=False,
        problems=[note],
        problem_codes=["unrecognized"],
        note=note,
    ).sealed()


# ---------------------------------------------------------------- 草稿标签
def draft_marker(body: str, *, lines: int = DRAFT_SCAN_LINES) -> str | None:
    """返回「文档开头含草稿标记」的那一行；``None`` 表示不是草稿（agent 应处理）。

    「开头」= 前 ``lines`` 个非空行。这样正文里偶然提到「草稿」不会让一份正式申请
    被永久跳过，而社员在模板第一行留下的 ``【草稿】`` / ``状态：草稿`` 都能识别。
    """
    seen = 0
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        seen += 1
        if seen > lines:
            break
        if any(hint in line for hint in DRAFT_HINTS):
            return raw_line
    return None


def is_draft(body: str, *, lines: int = DRAFT_SCAN_LINES) -> bool:
    return draft_marker(body, lines=lines) is not None


# ---------------------------------------------------------------- 字段解析
def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_fields(body: str) -> dict[str, str]:
    """从正文里抽出 ``字段：值``；容忍列表符号、加粗、全半角冒号。"""
    out: dict[str, str] = {}
    for m in FIELD_RE.finditer(body or ""):
        value = m.group(2).strip().strip("*_` ").strip()
        if value in {
            "",
            "-",
            "—",
            "（必填）",
            "(必填)",
            "（可不填）",
            "(可不填)",
            "（可选）",
            "(可选)",
        }:
            value = ""
        out.setdefault(m.group(1), value)
    return out


def cn_numeral_to_int(text: str) -> int | None:
    """把「八」「十二」「二十」「二十三」「一百」「一百二十」转成 int；认不出来返回 None。

    **认不出来就返回 None**，让上层退回：宁可让社员改一次，也不要猜错时间/人数。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "百" in text:  # 一百 / 一百二十 / 两百
        head, _, tail = text.partition("百")
        hundreds = cn_numeral_to_int(head) if head else 1
        if hundreds is None:
            return None
        rest = cn_numeral_to_int(tail) if tail else 0
        return None if rest is None else hundreds * 100 + rest
    if "十" in text:
        head, _, tail = text.partition("十")
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS[head] if head else 1
        ones = _CN_DIGITS[tail] if tail else 0
        return tens * 10 + ones
    digits = [_CN_DIGITS.get(ch) for ch in text]
    if len(digits) == 1 and digits[0] is not None:
        return digits[0]
    return None


def cn_numerals_to_digits(text: str) -> str:
    """把「两点」「八时」「九月」「二十三日」里的中文数字换成阿拉伯数字。

    认不出来的原样留着（后面会当解析失败处理，不会猜）。
    """

    def repl(match: re.Match[str]) -> str:
        value = cn_numeral_to_int(match.group(1))
        return str(value) if value is not None else match.group(1)

    return _CN_DATE_RE.sub(repl, _CN_HOUR_RE.sub(repl, text or ""))


def normalize_date(raw: str, now: datetime) -> tuple[datetime | None, str]:
    """宽松解析日期，返回 (日期, 规范化说明)。「九月二十三日」这种中文数字也认。"""
    text = cn_numerals_to_digits((raw or "").strip())
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
        if not m:
            return None, ""
        y, mo, d = now.year, int(m.group(1)), int(m.group(2))
    try:
        dt = datetime(y, mo, d, tzinfo=CN)
    except ValueError:
        return None, ""
    if dt.date() < (now - timedelta(days=180)).date():  # 只写月日且已过去 → 明年
        try:
            dt = dt.replace(year=y + 1)
        except ValueError:
            return None, ""
    fix = "" if text.startswith(f"{dt:%Y-%m-%d}") else f"日期「{text}」→ {dt:%Y-%m-%d}"
    return dt, fix


def _parse_clock(token: str, hint: str) -> tuple[int | None, str]:
    """把 ``16:10`` / ``4点`` / ``4点半`` / ``16时20`` 解析成分钟。

    第二个返回值是「12 小时制歧义」的提示；无提示又只写 1~7 点时按下午理解
    （没人早上七点借教室搞活动），但必须打 WARNING 让人自己确认。
    """
    m = re.match(r"^(\d{1,2})\s*[:：点时]\s*(\d{0,2})\s*分?\s*(半)?$", token.strip())
    if not m:
        return None, ""
    hour, minute, half = int(m.group(1)), m.group(2), m.group(3)
    mins = 30 if half else (int(minute) if minute else 0)
    if hour > 24 or mins >= 60:
        return None, ""
    if hint in {"下午", "晚上", "傍晚"}:
        if hour < 12:
            hour += 12
        return hour * 60 + mins, ""
    if hint in {"上午", "早上", "早晨"}:
        return (0 if hour == 12 else hour) * 60 + mins, ""
    if hint == "中午":
        return hour * 60 + mins, ""
    token_text = token.strip()
    if 0 < hour <= 7:  # 无上下午提示且只写 1~7 点 → 按下午/晚上理解
        warn = f"时间「{token_text}」没写上下午，agent 按 {_hm((hour + 12) * 60 + mins)} 处理（若是早上请写「上午{hour}点」）"
        return (hour + 12) * 60 + mins, warn
    if 8 <= hour <= 9:  # 早八晚八都可能，保留原样但提醒
        warn = (
            f"时间「{token_text}」没写上下午，agent 按 {_hm(hour * 60 + mins)} 处理"
            f"（若是晚上请写「晚上{hour}点」或 20:00）"
        )
        return hour * 60 + mins, warn
    return hour * 60 + mins, ""


HINT_WORDS = ("上午", "早上", "早晨", "中午", "下午", "晚上", "傍晚")


def _leading_hint(text: str, index: int) -> str:
    """取 ``text[:index]`` 末尾的「上午/下午/晚上」提示词（允许中间有空格，没有则空串）。

    社员写「晚上 8点到9点」时，空格不能让提示词丢掉——否则会被当成早上 8 点。
    """
    end = index
    while end > 0 and text[end - 1].isspace():
        end -= 1
    for word in HINT_WORDS:
        if text[max(0, end - len(word)) : end] == word:
            return word
    return ""


@dataclass
class TimeResult:
    """时间段解析结果。"""

    start: int | None = None
    end: int | None = None
    fix: str = ""
    warnings: tuple[str, ...] = ()


def normalize_time(raw: str) -> TimeResult:
    """宽松解析时间段，允许 ``下午4点-6点`` / ``八点到九点`` / ``8:30~9:30`` / ``16:10-18:00``。"""
    text = cn_numerals_to_digits((raw or "").strip())
    hint = ""
    m_hint = re.match(r"^(上午|早上|早晨|中午|下午|晚上|傍晚)", text)
    if m_hint:
        hint = m_hint.group(1)
    m = TIME_HINT_RE.search(text)
    if not m:
        return TimeResult()
    start, w1 = _parse_clock(m.group(1), hint)
    end, w2 = _parse_clock(m.group(2), hint)
    if start is None or end is None:
        return TimeResult()
    fix = (
        ""
        if re.fullmatch(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}", text)
        else f"时间「{text}」→ {_hm(start)}-{_hm(end)}"
    )
    return TimeResult(start, end, fix, tuple(w for w in (w1, w2) if w))


def normalize_campus(raw: str) -> tuple[str, str, str]:
    """返回 (规范校区名, 学校代码, 规范化说明)。"""
    text = (raw or "").strip()
    for alias, (name, code) in CAMPUSES.items():
        if alias in text:
            fix = "" if text == name else f"校区「{text}」→ {name}"
            return name, code, fix
    return "", "", ""


def normalize_people(raw: str) -> tuple[int | None, str, str]:
    """返回 (人数, 规范化说明, 问题)。空值由调用方决定默认值。"""
    text = (raw or "").strip()
    if not text:
        return None, "", ""
    m = re.search(r"\d+", text)
    if not m:
        # 「三十人」「二十位」这种也认；认不出来就退回（不猜）
        guess = cn_numeral_to_int(re.sub(r"[人位个名]$", "", text).strip())
        if guess is None:
            return None, "", f"「人数」不是数字：{text}"
        return guess, f"人数「{text}」→ {guess}", ""
    value = int(m.group(0))
    if value <= 0:
        return None, "", f"「人数」必须大于 0：{text}"
    fix = "" if text == str(value) else f"人数「{text}」→ {value}"
    return value, fix, ""


def title_problem(title: str) -> str | None:
    """文档标题即活动名称；返回不合规原因（``None`` 表示合规）。"""
    t = (title or "").strip()
    if t in TITLE_BAD_EXACT:
        return "文档标题为空（语雀会显示成「无标题文档」），请把标题写成活动名称"
    for hint in TITLE_BAD_HINTS:
        if hint in t:
            return f"文档标题「{t}」和系统文档重名，请把标题写成活动名称"
    if len(t) > MAX_TITLE_LEN:
        return f"文档标题有 {len(t)} 个字，太长；请用简短的活动名称"
    return None


def period_table() -> list[tuple[int, str, str]]:
    """(节次, 真实开始, 真实结束) —— 给文档/帮助信息用。"""
    return [(i + 1, _hm(PERIOD_STARTS[i]), _hm(PERIOD_ENDS[i])) for i in range(len(PERIOD_STARTS))]


# ---------------------------------------------------------------- 节次推算
def subtract_lunch(start: int, end: int) -> list[tuple[int, int]]:
    """从 ``[start, end)`` 里剔除 12:00-14:00 吃饭时间，返回剩下的可用区间。

    午饭时段不是「违规」，而是「这段不需要借教室」：

    - 完全落在午饭内 → 返回空 → 不需要借教室，退回；
    - 只跨一边（13:00-15:00）→ 返回 ``[(14:00, 15:00)]``；
    - 两边都跨（11:00-15:00）→ 返回两段，节次取并集（第 4-5 节，一次申请就够）。
    """
    out: list[tuple[int, int]] = []
    if start < LUNCH_START:
        out.append((start, min(end, LUNCH_START)))
    if end > LUNCH_END:
        out.append((max(start, LUNCH_END), end))
    return [(s, e) for s, e in out if e > s]


def derive_periods_multi(segments: list[tuple[int, int]]) -> tuple[int, int] | None:
    """多个可用区间覆盖了哪些节次；返回 ``(起始节, 结束节)``。"""
    used: set[int] = set()
    for start, end in segments:
        used.update(p for p, ps, pe in PERIODS if min(end, pe) > max(start, ps))
    if not used:
        return None
    return min(used), max(used)


def derive_periods(start: int, end: int) -> tuple[int, int] | None:
    """单个时间区间覆盖了哪些节次。"""
    return derive_periods_multi([(start, end)])


def period_span(periods: tuple[int, int]) -> str:
    a, b = periods
    return f"{a}-{b}" if a != b else str(a)


# ---------------------------------------------------------------- 判定
def evaluate(
    fields: dict[str, str],
    *,
    title: str,
    author: str,
    now: datetime,
    default_people: int = DEFAULT_PEOPLE,
    default_borrow_type: str = DEFAULT_BORROW_TYPE,
) -> Verdict:
    """四档判断：ok / normalized / rejected（unrecognized 由 :func:`salvage` 产出）。"""
    v = Verdict(fields=dict(fields))
    problems: list[str] = []
    codes: list[str] = []
    fixes: list[str] = []
    warnings: list[str] = []

    def bad(code: str, text: str) -> None:
        """同时记「给人看的文案」和「给机器去重的问题码」。"""
        problems.append(text)
        codes.append(code)

    # --- 标题（= 活动名称）---
    if problem := title_problem(title):
        bad("bad_title", problem)

    # --- 申请人：没填就用文档创建者（agent 的职责是推断，不是追问）---
    if not fields.get("申请人") and author:
        v.fields["申请人"] = author
        fixes.append(f"申请人缺失 → 用文档创建者「{author}」")
    for name in REQUIRED_FIELDS:
        if not v.fields.get(name):
            bad(f"missing_field:{name}", f"「{name}」为空，且无法推断")

    # --- 日期 ---
    date, date_fix = normalize_date(v.fields.get("活动日期", ""), now)
    if date_fix:
        fixes.append(date_fix)
    if date is None and v.fields.get("活动日期"):
        bad("bad_date", f"「活动日期」无法解析：{v.fields['活动日期']}")

    # --- 时间 ---
    raw_time = v.fields.get("活动时间", "")
    if len(TIME_HINT_RE.findall(cn_numerals_to_digits(raw_time))) > 1:
        # 两个时间段（「16:10-18:00 或 19:00-21:00」）如果只取第一个就是**静默判错**，
        # 宁可退回让社员拆成两次申请。
        bad("multiple_time_ranges", f"「活动时间」里有多个时间段：{raw_time}；请只写一个")
    parsed = normalize_time(raw_time)
    start, end = parsed.start, parsed.end
    if parsed.fix:
        fixes.append(parsed.fix)
    warnings.extend(parsed.warnings)
    if start is None or end is None:
        if v.fields.get("活动时间"):
            bad("bad_time", f"「活动时间」无法解析：{v.fields['活动时间']}")
    else:
        if start >= end:
            bad("time_reversed", f"活动时间前后颠倒：{_hm(start)}-{_hm(end)}（应写「开始-结束」）")
        if start < DAY_START or end > DAY_END:
            bad(
                "out_of_window",
                f"活动时间 {_hm(start)}-{_hm(end)} 超出可申请时段 {_hm(DAY_START)}-{_hm(DAY_END)}",
            )
        segments = subtract_lunch(start, end)
        if not segments:
            bad(
                "lunch_only",
                f"活动时间 {_hm(start)}-{_hm(end)} 完全落在 {_hm(LUNCH_START)}-{_hm(LUNCH_END)} "
                "吃饭时间内，不需要借教室",
            )
        elif segments != [(start, end)]:
            v.lunch_skipped = True
        if end - start >= SPAN_WARN_MINUTES:
            warnings.append(
                f"活动时间 {_hm(start)}-{_hm(end)} 长达 {(end - start) / 60:.1f} 小时，"
                "请确认没有把结束时间写错"
            )

    # --- 校区 ---
    campus, campus_code, campus_fix = normalize_campus(v.fields.get("校区", ""))
    if campus_fix:
        fixes.append(campus_fix)
    if not campus and v.fields.get("校区"):
        bad("bad_campus", f"「校区」不认识：{v.fields['校区']}（应填 鼓楼/浦口/仙林/苏州）")

    # --- 人数 ---
    people, people_fix, people_problem = normalize_people(v.fields.get("人数", ""))
    if people_fix:
        fixes.append(people_fix)
    if people_problem:
        bad("bad_people", people_problem)
    if people is not None and people > PEOPLE_WARN_MAX:
        warnings.append(f"人数 {people} 偏多，请确认教室坐得下")

    # --- 节次推算 + 48 小时提前量 ---
    if start is not None and end is not None and not problems:
        periods = derive_periods_multi(subtract_lunch(start, end))
        if periods is None:
            bad(
                "period_mismatch",
                f"活动时间 {_hm(start)}-{_hm(end)} 对不上任何节次，请按课表时间填写",
            )
        else:
            span = period_span(periods)
            if v.lunch_skipped:
                fixes.append(
                    f"{_hm(LUNCH_START)}-{_hm(LUNCH_END)} 吃饭时间不需要借教室，"
                    f"按可用时段推得第 {span} 节"
                )
            if date is not None:
                act_start = date.replace(hour=start // 60, minute=start % 60)
                gap = act_start - now
                if gap < timedelta(hours=ADVANCE_HOURS):
                    if gap <= timedelta(0):
                        bad(
                            "in_past",
                            f"活动开始时间 {act_start:%Y-%m-%d %H:%M} 已经过去，无法借用",
                        )
                    else:
                        bad(
                            "too_soon",
                            f"活动开始时间 {act_start:%Y-%m-%d %H:%M} 距现在仅 "
                            f"{gap.total_seconds() / 3600:.1f} 小时，不足 {ADVANCE_HOURS} 小时",
                        )
                elif (date.date() - now.date()).days > EXPIRE_WARN_DAYS:
                    warnings.append(
                        f"活动日期距今还有 {(date.date() - now.date()).days} 天，"
                        f"超过 {EXPIRE_WARN_DAYS} 天，请确认该时段真的可以借用"
                    )
                if people is None:
                    people = default_people  # 人数不详 → 默认值（JSON 里 people_source=default）
                v.activity = Activity(
                    name=title.strip(),
                    date=f"{date:%Y-%m-%d}",
                    start=_hm(start),
                    end=_hm(end),
                    period=span,
                    period_start=periods[0],
                    period_end=periods[1],
                    campus=campus,
                    campus_code=campus_code,
                    building=(v.fields.get("教学楼") or "").strip(),
                    room=(v.fields.get("教室") or "").strip(),
                    people=people,
                    people_source="document" if v.fields.get("人数") else "default",
                    borrow_type=default_borrow_type,
                )

    v.problems = problems
    v.problem_codes = codes
    v.warnings = warnings
    v.fixes = fixes
    v.ok = not problems
    v.tier = "rejected" if problems else ("normalized" if (fixes or warnings) else "ok")
    if not v.ok:
        v.activity = None
    v.note = "；".join(problems) if problems else ""
    return v.sealed()


def salvage(
    body: str,
    *,
    title: str,
    author: str,
    now: datetime,
    default_people: int = DEFAULT_PEOPLE,
) -> Verdict | None:
    """没用模板的文档：试着从正文里提取「日期 + 时间 + 校区」，能提取就当申请处理。

    提取不到就返回 ``None``（pipeline 会判为 unrecognized 并通知本人）。
    """
    text = body or ""
    normalized = cn_numerals_to_digits(text)
    m_date = DATE_HINT_RE.search(text)
    m_time = TIME_HINT_RE.search(normalized)
    if len(TIME_HINT_RE.findall(normalized)) > 1:
        return Verdict(
            tier="rejected",
            ok=False,
            problems=["正文里有多个时间段，agent 分不清你要借哪一段；请只写一个（如 16:10-18:00）"],
            problem_codes=["multiple_time_ranges"],
        ).sealed()
    date, _ = normalize_date(m_date.group(1) if m_date else "", now)
    # 注意：TIME_HINT_RE 只匹配到「8点到9点」，前面的「晚上/下午」不在匹配里，
    # 必须自己从原文里把提示词带上，否则「晚上8点到9点」会被当成早上 8 点。
    time_text = _leading_hint(normalized, m_time.start()) + m_time.group(0) if m_time else ""
    parsed = normalize_time(time_text)
    campus, _, _ = normalize_campus(text)
    if not (date and parsed.start is not None and parsed.end is not None and campus):
        return None
    fields = {
        "申请人": author,
        "活动日期": f"{date:%Y-%m-%d}",
        "活动时间": f"{_hm(parsed.start)}-{_hm(parsed.end)}",
        "校区": campus,
        "人数": "",
        "教学楼": "",
        "教室": "",
    }
    v = evaluate(fields, title=title, author=author, now=now, default_people=default_people)
    if v.activity is not None:
        v.fixes.insert(0, "未使用模板，但正文里能提取到日期/时间/校区 → 已按申请处理")
        v.tier = "normalized"
    return v
