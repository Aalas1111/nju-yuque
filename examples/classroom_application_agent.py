"""教室借用申请 · 知识库自动维护 agent（单库版）

设计原则：**这是一个 agent，不是死程序。** 对每份文档先判断它属于哪一档：

| 档位 | 判断 | 动作 |
|---|---|---|
| 合规 | 字段齐全、时间/日期/校区能直接对上 | 规范化 → 提交 → 写审批日志 |
| 可处理为合规 | 写法不标准（`9月16日`、`下午4点-6点`、`仙林校区`）、\n|            | 可选字段缺失、申请人可由文档创建者推断 | 自动规范化后按合规处理，日志注明改了什么 |
| 绝对不合规 | 超期 / 撞吃饭时间 / 对不上节次 / 关键信息缺失且推断不出 | 状态改「已退回」，日志写原因 |
| 不像申请 | 没用模板且提取不到「日期+时间+校区」 | 删除 |

agent 会自动生成提交所需的**借用节次**（由活动时间推算）、校区代码等。

默认 **dry-run**；`--apply` 才真正写。

用法::

    export YUQUE_HOME=~/.yuque
    uv run python examples/classroom_application_agent.py --repo <group/slug>
    uv run python examples/classroom_application_agent.py --repo <group/slug> --apply
    uv run python examples/classroom_application_agent.py --repo <group/slug> --approve "<标题或slug>"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from nju_yuque.api import YuqueApi
from nju_yuque.errors import YuqueError
from nju_yuque.models import Doc
from nju_yuque.session import Credentials

# ---------------------------------------------------------------- 知识库约定
GUIDE_TITLE = "00-指导文档（必读）"
LOG_TITLE = "审批日志"  # 申请文档下由 agent 维护的子文档
ARCHIVE_TITLE = "归档区"
# 周目录命名：0914-0920（MMDD-MMDD），后面允许跟任意说明文字
WEEK_TITLE_RE = re.compile(r"^(\d{2})(\d{2})\s*[-~～]\s*(\d{2})(\d{2})")

# ---------------------------------------------------------------- 状态机
STATUS_PENDING = "待提交"
STATUS_SUBMITTED = "已提交（待审核通过）"
STATUS_APPROVED = "已通过（本文档已归档，禁止再次修改）"
STATUS_REJECTED = "已退回（修改后请把状态改为待提交）"
STATUS_RE = re.compile(r"^([ \t>*\-•]*\**\s*状态\s*\**\s*[:：][ \t]*)(.*)$", re.MULTILINE)

# ---------------------------------------------------------------- 业务规则
ADVANCE_HOURS = 48  # 提前量
DAY_START, DAY_END = 8 * 60, 22 * 60 + 20  # 可申请时段 08:00 - 22:20
LUNCH_START, LUNCH_END = 12 * 60, 14 * 60  # 吃饭时间，不可重叠

# 真实上课时间（仅作对照）：第 1 节 08:00-08:50，第 7 节 16:10-17:00 ……
PERIOD_STARTS = [480, 540, 610, 670, 840, 900, 970, 1030, 1110, 1170, 1230, 1290]
PERIOD_ENDS = [530, 590, 660, 720, 890, 950, 1020, 1080, 1160, 1220, 1280, 1340]

# 申请用的「一小时一档」：档位起点 = 真实起点向下取到 30 分钟，档长 60 分钟。
# 于是 第1节 = 08:00-09:00、第7节 = 16:00-17:00（课间那 10 分钟不纠结），
# 晚上 第9节 = 18:30-19:30（起点本来就在半点）。
PERIODS: list[tuple[int, int, int]] = [
    (i + 1, (s // 30) * 30, (s // 30) * 30 + 60) for i, s in enumerate(PERIOD_STARTS)
]

CAMPUSES: dict[str, tuple[str, str]] = {  # 别名 -> (规范名, 学校代码)
    "鼓楼": ("鼓楼", "1"),
    "浦口": ("浦口", "2"),
    "仙林": ("仙林", "3"),
    "苏州": ("苏州", "4"),
}

# 提交时的缺省值（社团统一申请，联系人统一用老师电话）
DEFAULT_CONTACT = os.environ.get("CRB_CONTACT", "13800000000")
DEFAULT_PEOPLE = 30  # 人数不详时按「足够少，随便借一间」处理

CN = timezone(timedelta(hours=8))

REQUIRED_FIELDS = ("活动名称", "申请人", "活动日期", "活动时间", "校区")
FILL_FIELDS = ("教学楼", "教室")
ALL_FIELDS = ("状态", *REQUIRED_FIELDS, *FILL_FIELDS)

FIELD_RE = re.compile(
    # 注意：[ \t] 而不是 \s —— \s 会吃掉换行，导致空值字段把下一行当成自己的值
    r"^[ \t>*\-•]*\**[ \t]*(" + "|".join(ALL_FIELDS) + r")[ \t]*\**[ \t]*[:：][ \t]*(.*?)[ \t]*$",
    re.MULTILINE,
)
DATE_HINT_RE = re.compile(
    r"(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}|\d{1,2}\s*[-/.月]\s*\d{1,2})"
)
TIME_HINT_RE = re.compile(
    r"(\d{1,2}\s*[:：点时]\s*(?:\d{1,2}|半)?)"
    r"\s*(?:-|~|～|—|到|至)\s*"
    r"(\d{1,2}\s*[:：点时]\s*(?:\d{1,2}|半)?)"
)


# ---------------------------------------------------------------- 数据结构
@dataclass
class Verdict:
    action: str  # keep / move / delete / skip
    tier: str = ""  # ok / normalized / rejected / orphan
    target_title: str = ""
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    derived: dict[str, object] = field(default_factory=dict)
    note: str = ""
    new_status: str = ""
    lunch_skipped: bool = False  # 活动跨了午饭时段，节次需按可用区间取并集


# ---------------------------------------------------------------- 规范化
def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def set_status(body: str, new_status: str) -> str:
    """把文档首行的「状态：xxx」改成新状态；没有这一行就补在开头。"""
    if STATUS_RE.search(body or ""):
        return STATUS_RE.sub(lambda m: m.group(1) + new_status, body, count=1)
    return f"状态：{new_status}\n\n{body or ''}"


def parse_fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in FIELD_RE.finditer(body or ""):
        value = m.group(2).strip().strip("*_` ").strip()
        if value in {"", "-", "—", "（必填）", "(必填)", "（可不填）", "(可不填)"}:
            value = ""
        out.setdefault(m.group(1), value)
    return out


def normalize_date(raw: str, now: datetime) -> tuple[datetime | None, str]:
    """宽松解析日期，返回 (日期, 规范化说明)。"""
    text = (raw or "").strip()
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


def _parse_clock(token: str, hint: str) -> int | None:
    """把 `16:10` / `4点` / `4点半` / `16时20` 解析成分钟。"""
    m = re.match(r"^(\d{1,2})\s*[:：点时]\s*(\d{0,2})\s*分?\s*(半)?$", token.strip())
    if not m:
        return None
    hour, minute, half = int(m.group(1)), m.group(2), m.group(3)
    mins = 30 if half else (int(minute) if minute else 0)
    if hour > 24 or mins >= 60:
        return None
    if hint in {"下午", "晚上", "傍晚"} and hour < 12:
        hour += 12
    elif hint in {"上午", "早上", "早晨"} and hour == 12:
        hour = 0
    elif not hint and 0 < hour <= 7:  # 无提示又只有 1~7 点 → 按下午理解
        hour += 12
    return hour * 60 + mins


def normalize_time(raw: str) -> tuple[int | None, int | None, str]:
    """宽松解析时间段，返回 (开始分钟, 结束分钟, 规范化说明)。"""
    text = (raw or "").strip()
    hint = ""
    m_hint = re.match(r"^(上午|早上|早晨|中午|下午|晚上|傍晚)", text)
    if m_hint:
        hint = m_hint.group(1)
    m = TIME_HINT_RE.search(text)
    if not m:
        return None, None, ""
    start = _parse_clock(m.group(1), hint)
    end = _parse_clock(m.group(2), hint)
    if start is None or end is None:
        return None, None, ""
    fix = (
        ""
        if re.fullmatch(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}", text)
        else (f"时间「{text}」→ {_hm(start)}-{_hm(end)}")
    )
    return start, end, fix


def normalize_campus(raw: str) -> tuple[str, str, str]:
    """返回 (规范校区名, 学校代码, 规范化说明)。"""
    text = (raw or "").strip()
    for alias, (name, code) in CAMPUSES.items():
        if alias in text:
            fix = "" if text == name else f"校区「{text}」→ {name}"
            return name, code, fix
    return "", "", ""


def subtract_lunch(start: int, end: int) -> list[tuple[int, int]]:
    """从 [start, end) 里剔除 12:00-14:00 吃饭时间，返回剩下的可用区间。

    午饭时段不是「违规」，而是「这段不需要借教室」：
    - 完全落在午饭内 → 返回空 → 不需要借教室，退回
    - 只跨一边（13:00-15:00）→ 返回 [(14:00, 15:00)]
    - 两边都跨（11:00-15:00）→ 返回两段，节次取并集（第 4-5 节）
    """
    out: list[tuple[int, int]] = []
    if start < LUNCH_START:
        out.append((start, min(end, LUNCH_START)))
    if end > LUNCH_END:
        out.append((max(start, LUNCH_END), end))
    return [(s, e) for s, e in out if e > s]


def derive_periods_multi(segments: Sequence[tuple[int, int]]) -> tuple[int, int] | None:
    """多个可用区间覆盖了哪些节次；返回 (起始节, 结束节)。

    按「一小时一档」匹配：第 1 节 = 08:00-09:00、第 7 节 = 16:00-17:00，
    课间那 10 分钟不计。跨午饭时取各区间节次的并集：
    11:00-12:00 + 14:00-15:00 → 第 4-5 节（学校表单本来就是「起节-止节」一个区间）。
    """
    used: set[int] = set()
    for start, end in segments:
        used.update(p for p, ps, pe in PERIODS if min(end, pe) > max(start, ps))
    if not used:
        return None
    return min(used), max(used)


def derive_periods(start: int, end: int) -> tuple[int, int] | None:
    """单个时间区间覆盖了哪些节次。"""
    return derive_periods_multi([(start, end)])


# ---------------------------------------------------------------- 判定
def evaluate(fields: dict[str, str], *, title: str, author: str, now: datetime) -> Verdict:
    """三档判断：ok / normalized / rejected。"""
    v = Verdict(action="keep", fields=dict(fields))
    status = fields.get("状态", "")
    if status != STATUS_PENDING:
        v.tier = "skip"
        v.note = f"状态为「{status or '缺失'}」，跳过（只处理「{STATUS_PENDING}」）"
        return v

    # --- 必填字段：能推断的就推断（agent 的职责） ---
    if not fields.get("活动名称") and title:
        v.fields["活动名称"] = title
        v.fixes.append(f"活动名称缺失 → 用文档标题「{title}」")
    if not fields.get("申请人") and author:
        v.fields["申请人"] = author
        v.fixes.append(f"申请人缺失 → 用文档创建者「{author}」")
    for name in REQUIRED_FIELDS:
        if not v.fields.get(name):
            v.problems.append(f"「{name}」为空，且无法推断")

    # --- 日期 ---
    date, date_fix = normalize_date(v.fields.get("活动日期", ""), now)
    if date_fix:
        v.fixes.append(date_fix)
    if date is None:
        if v.fields.get("活动日期"):
            v.problems.append(f"「活动日期」无法解析：{v.fields['活动日期']}")

    # --- 时间 ---
    start, end, time_fix = normalize_time(v.fields.get("活动时间", ""))
    orig_start = start  # 48 小时按「活动真实开始时间」算，不受剔除午饭影响
    if time_fix:
        v.fixes.append(time_fix)
    if start is None or end is None:
        if v.fields.get("活动时间"):
            v.problems.append(f"「活动时间」无法解析：{v.fields['活动时间']}")
        else:
            v.problems.append("「活动时间」为空")
    else:
        if start >= end:
            v.problems.append(f"活动时间前后颠倒：{_hm(start)}-{_hm(end)}")
        if start < DAY_START or end > DAY_END:
            v.problems.append(
                f"活动时间 {_hm(start)}-{_hm(end)} 超出可申请时段 {_hm(DAY_START)}-{_hm(DAY_END)}"
            )
        # 剔除吃饭时间：不是「违规」，而是这段时间不需要借教室
        if not subtract_lunch(start, end):
            v.problems.append(
                f"活动时间 {_hm(start)}-{_hm(end)} 完全落在 {_hm(LUNCH_START)}-{_hm(LUNCH_END)} "
                "吃饭时间内，不需要借教室"
            )
        elif subtract_lunch(start, end) != [(start, end)]:
            v.lunch_skipped = True

    # --- 校区 ---
    campus, campus_code, campus_fix = normalize_campus(v.fields.get("校区", ""))
    if campus_fix:
        v.fixes.append(campus_fix)
    if not campus and v.fields.get("校区"):
        v.problems.append(f"「校区」不认识：{v.fields['校区']}（应填 鼓楼/浦口/仙林/苏州）")
    elif not campus:
        v.problems.append("「校区」为空（鼓楼/浦口/仙林/苏州）")

    # --- 节次推算 ---
    if start is not None and end is not None and not v.problems:
        # 匹配用的是「一小时一档」，档位起点已经把课间 10 分钟舍掉了
        periods = derive_periods_multi(subtract_lunch(start, end))
        if periods is None:
            v.problems.append(f"活动时间 {_hm(start)}-{_hm(end)} 对不上任何节次")
        else:
            ksjc, jsjc = periods
            span = f"{ksjc}-{jsjc}" if ksjc != jsjc else str(ksjc)
            if v.lunch_skipped:
                v.fixes.append(
                    f"{_hm(LUNCH_START)}-{_hm(LUNCH_END)} 吃饭时间不需要借教室，"
                    f"按可用时段推得第 {span} 节"
                )
            v.derived = {
                "日期": f"{date:%Y-%m-%d}" if date else "",
                "开始": _hm(start),
                "结束": _hm(end),
                "借用节次": span,
                "KSJC": ksjc,
                "JSJC": jsjc,
                "校区": campus,
                "XXXQDM": campus_code,
                "教学楼": v.fields.get("教学楼") or "(随机)",
                "教室": v.fields.get("教室") or "(随机)",
                "人数": DEFAULT_PEOPLE,
                "联系电话": DEFAULT_CONTACT,
            }
            # --- 提前 48 小时（按活动真实开始时间） ---
            if date is not None and orig_start is not None:
                act_start = date.replace(hour=orig_start // 60, minute=orig_start % 60)
                gap = act_start - now
                if gap < timedelta(hours=ADVANCE_HOURS):
                    hours = gap.total_seconds() / 3600
                    v.problems.append(
                        f"活动开始时间 {act_start:%Y-%m-%d %H:%M} 距现在仅 {hours:.1f} 小时，"
                        f"不足 {ADVANCE_HOURS} 小时"
                    )

    v.ok = not v.problems
    v.tier = "rejected" if v.problems else ("normalized" if v.fixes else "ok")
    v.new_status = STATUS_SUBMITTED if v.ok else STATUS_REJECTED
    return v


def salvage(body: str, *, title: str, author: str, now: datetime) -> Verdict | None:
    """没用模板的文档：试着从中提取「日期 + 时间 + 校区」，能提取就当申请处理。"""
    text = body or ""
    date, _ = normalize_date(
        DATE_HINT_RE.search(text).group(1) if DATE_HINT_RE.search(text) else "", now
    )
    start, end, _ = normalize_time(
        TIME_HINT_RE.search(text).group(0) if TIME_HINT_RE.search(text) else ""
    )
    campus, _, _ = normalize_campus(text)
    if not (date and start is not None and campus):
        return None
    fields = {
        "状态": STATUS_PENDING,
        "活动名称": title,
        "申请人": author,
        "活动日期": f"{date:%Y-%m-%d}",
        "活动时间": f"{_hm(start)}-{_hm(end)}",
        "校区": campus,
    }
    v = evaluate(fields, title=title, author=author, now=now)
    v.fixes.insert(0, "未使用模板，但正文里能提取到日期/时间/校区 → 已按申请处理")
    v.tier = "normalized"
    return v


# ---------------------------------------------------------------- 知识库操作
class Kb:
    def __init__(self, repo: str) -> None:
        cred = Credentials.load()
        if not cred.is_token:
            raise SystemExit("需要令牌模式：请先 `yuque login --token <写权限令牌>`")
        self.repo = repo
        self.api = YuqueApi(cred.host, cred.token, group=cred.group)
        self.now = datetime.now(CN)

    def close(self) -> None:
        self.api.close()

    # -- 读 -----------------------------------------------------------
    def toc(self) -> list:
        return self.api.toc(self.repo)

    def docs(self) -> list[Doc]:
        return self.api.docs(self.repo)

    def read(self, doc_id: int) -> Doc:
        return self.api.doc(self.repo, str(doc_id))

    @staticmethod
    def node_path(items: list) -> dict[str, list[str]]:
        by_uuid = {i.uuid: i for i in items}
        out: dict[str, list[str]] = {}
        for item in items:
            chain, parent = [], item.parent_uuid
            while parent and parent in by_uuid:
                chain.append(by_uuid[parent].title)
                parent = by_uuid[parent].parent_uuid
            out[item.uuid] = list(reversed(chain))
        return out

    def find_log(self, app_node_uuid: str) -> Doc | None:
        """找申请文档下已有的「审批日志」子文档。"""
        items = self.toc()
        for item in items:
            if item.title != LOG_TITLE or item.parent_uuid != app_node_uuid:
                continue
            return next((d for d in self.docs() if d.id == item.doc_id), None)
        return None

    # -- 写 -----------------------------------------------------------
    def move(self, node_uuid: str, parent_uuid: str) -> None:
        self.api.toc_move(self.repo, node_uuid=node_uuid, target_uuid=parent_uuid)

    def mount(self, doc_id: int, parent_uuid: str) -> None:
        self.api.toc_add(self.repo, doc_ids=[doc_id], target_uuid=parent_uuid)

    def delete(self, doc_id: int) -> None:
        self.api.delete_doc(self.repo, doc_id)

    def set_status(self, doc: Doc, status: str) -> None:
        self.api.update_doc(
            self.repo, doc.id, body=set_status(self.read(doc.id).body or "", status)
        )

    def write_log(self, app: Doc, app_node_uuid: str, v: Verdict) -> None:
        """维护/追加「审批日志」。"""
        stamp = self.now.strftime("%Y-%m-%d %H:%M")
        if v.ok:
            head = f"### {stamp} · 校验通过，已提交"
            lines = [
                "- 结论：**通过**",
                f"- 活动：{v.fields.get('活动名称', '')} / {v.derived.get('日期', '')} "
                f"{v.derived.get('开始', '')}-{v.derived.get('结束', '')}",
                f"- 借用节次：**{v.derived.get('借用节次', '')}**（由活动时间推算）",
                f"- 校区：{v.derived.get('校区', '')}（{v.derived.get('XXXQDM', '')}）"
                f"　教学楼：{v.derived.get('教学楼', '')}　教室：{v.derived.get('教室', '')}",
                f"- 人数：{v.derived.get('人数', '')}（默认）　联系电话：{v.derived.get('联系电话', '')}",
                f"- 处理：已提交教室申请，当前状态 {STATUS_SUBMITTED}",
            ]
        elif v.tier == "skip":
            return
        else:
            head = f"### {stamp} · 退回修改"
            lines = (
                ["- 结论：**退回修改**", "- 原因："]
                + [f"  {i}. {p}" for i, p in enumerate(v.problems, 1)]
                + ["- 处理：请修改后把「状态」改为 `待提交`，agent 会重新校验"]
            )
        if v.fixes:
            lines += ["- agent 自动规范化："] + [f"  - {f}" for f in v.fixes]

        section = head + "\n" + "\n".join(lines) + "\n"
        log = self.find_log(app_node_uuid)
        if log is None:
            created = self.api.create_doc(
                self.repo,
                title=LOG_TITLE,
                body=(
                    "> ⚙️ 本文件由系统自动维护，请勿手动编辑。\n\n"
                    f"# 审批日志 · {app.title}\n\n{section}"
                ),
            )
            self.mount(created.id, app_node_uuid)
        else:
            self.api.update_doc(
                self.repo, log.id, body=(self.read(log.id).body or "") + "\n" + section
            )


# ---------------------------------------------------------------- 主流程
def _week_dir_for(week_dirs: list, date: datetime | None, now: datetime):
    if date is None:
        return None
    for wd in week_dirs:
        m = WEEK_TITLE_RE.match(wd.title)
        if not m:
            continue
        try:
            start = datetime.strptime(f"{now.year}-{m.group(1)}-{m.group(2)}", "%Y-%m-%d").replace(
                tzinfo=CN
            )
            end = datetime.strptime(f"{now.year}-{m.group(3)}-{m.group(4)}", "%Y-%m-%d").replace(
                tzinfo=CN
            )
        except ValueError:
            continue
        if start <= date <= end:
            return wd
    return None


def plan(kb: Kb) -> list[tuple[Doc, object, Verdict]]:
    items = kb.toc()
    path = Kb.node_path(items)
    doc_node = {i.doc_id: i for i in items if i.doc_id}
    week_dirs = [i for i in items if i.type == "TITLE" and WEEK_TITLE_RE.match(i.title)]
    structural = {GUIDE_TITLE, ARCHIVE_TITLE, LOG_TITLE}

    out = []
    for doc in kb.docs():
        node = doc_node.get(doc.id)
        in_archive = bool(node) and ARCHIVE_TITLE in path.get(node.uuid, [])

        if doc.title in structural:
            out.append((doc, node, Verdict("skip", tier="skip", note="结构性文档")))
            continue

        detail = kb.read(doc.id)
        body = detail.body or ""
        author = str((detail.creator or {}).get("name") or "")
        fields = parse_fields(body)

        if len(fields) >= 3:  # 用了模板
            v = evaluate(fields, title=doc.title, author=author, now=kb.now)
        else:
            v = salvage(body, title=doc.title, author=author, now=kb.now) or Verdict(
                "delete",
                tier="orphan",
                note=f"没用模板，也提取不到日期/时间/校区（识别到 {len(fields)} 个字段）",
            )

        if v.tier == "skip":
            out.append((doc, node, v))
            continue

        if v.action == "delete":
            out.append((doc, node, v))
            continue

        # 归属周目录
        date = None
        if v.derived.get("日期"):
            date = datetime.strptime(str(v.derived["日期"]), "%Y-%m-%d").replace(tzinfo=CN)
        target = _week_dir_for(week_dirs, date, kb.now)
        if target is None:
            v.problems.append("活动日期不在当前可申请的周目录范围内（只接受当前周与下一周）")
            v.ok = False
            v.tier = "rejected"
            v.new_status = STATUS_REJECTED
        elif in_archive or node is None or node.parent_uuid != target.uuid:
            v.action = "move"
            v.target_title = target.title
        out.append((doc, node, v))
    return out


def approve_doc(kb: Kb, key: str) -> None:
    """把一份申请推进到「已通过」（学校审核通过后调用）。"""
    doc = next((d for d in kb.docs() if d.slug == key or d.title == key), None)
    if doc is None:
        print(f"  ! 找不到文档：{key}")
        return
    kb.set_status(doc, STATUS_APPROVED)

    node = next((i for i in kb.toc() if i.doc_id == doc.id), None)
    if node is None:
        print(f"  ✓ {doc.title} → {STATUS_APPROVED}（不在目录中，未写日志）")
        return
    stamp = kb.now.strftime("%Y-%m-%d %H:%M")
    section = (
        f"### {stamp} · 学校审核通过\n"
        "- 结论：**已通过**\n"
        "- 处理：申请已通过，本文档归档，请勿再修改\n"
    )
    log = kb.find_log(node.uuid)
    if log:
        kb.api.update_doc(kb.repo, log.id, body=(kb.read(log.id).body or "") + "\n" + section)
    else:
        created = kb.api.create_doc(
            kb.repo,
            title=LOG_TITLE,
            body=f"> ⚙️ 本文件由系统自动维护，请勿手动编辑。\n\n# 审批日志 · {doc.title}\n\n{section}",
        )
        kb.mount(created.id, node.uuid)
    print(f"  ✓ {doc.title} → {STATUS_APPROVED}")


TIER_LABEL = {
    "ok": "合规",
    "normalized": "可处理为合规（已自动规范化）",
    "rejected": "绝对不合规（退回）",
    "orphan": "不像申请（删除）",
    "skip": "跳过",
}


def main() -> None:
    ap = argparse.ArgumentParser(description="教室申请知识库自动维护（默认 dry-run）")
    ap.add_argument("--repo", required=True, help="知识库 id 或 group/slug")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    ap.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="标题或slug",
        help="把指定申请推进到「已通过」（学校审核通过后调用；可重复）",
    )
    args = ap.parse_args()

    kb = Kb(args.repo)
    try:
        if args.approve:
            print(f"# KB={args.repo}  现在={kb.now:%Y-%m-%d %H:%M}")
            for key in args.approve:
                approve_doc(kb, key)
            return

        items = kb.toc()
        path = Kb.node_path(items)
        archive = next((i for i in items if i.type == "TITLE" and i.title == ARCHIVE_TITLE), None)

        print(f"# KB={args.repo}  现在={kb.now:%Y-%m-%d %H:%M} (UTC+8)  dry_run={not args.apply}\n")
        reports = plan(kb)
        for doc, node, v in reports:
            loc = " / ".join(path.get(node.uuid, [])) if node else "(不在目录)"
            print(f"## {doc.title}  (#{doc.id})")
            print(f"   位置: {loc or '(根)'}")
            print(
                f"   判定: 「{TIER_LABEL.get(v.tier, v.tier)}」 动作={v.action}"
                f"{(' → ' + v.target_title) if v.target_title else ''}"
            )
            if v.note:
                print(f"   说明: {v.note}")
            for f in v.fixes:
                print(f"   ↻ {f}")
            for p in v.problems:
                print(f"   ✗ {p}")
            if v.derived:
                print(f"   提交参数: {v.derived}")
            if v.new_status:
                print(f"   状态: {STATUS_PENDING} → {v.new_status}")
            print()

        if not args.apply:
            print("（dry-run，未做任何修改；加 --apply 执行）")
            return

        print("=" * 60, "\n开始执行")
        for doc, node, v in reports:
            if v.action == "delete":
                kb.delete(doc.id)
                print(f"  ✗ 已删除：{doc.title}（{v.note}）")
            elif v.action == "move":
                target = next(i for i in items if i.title == v.target_title)
                if node is None:
                    kb.mount(doc.id, target.uuid)
                else:
                    kb.move(node.uuid, target.uuid)
                print(f"  → 已移动：{doc.title} → {v.target_title}")

        for doc, _node, v in reports:
            if not v.new_status:
                continue
            try:
                kb.set_status(doc, v.new_status)
                print(f"  ⇄ 状态：{doc.title} → {v.new_status}")
            except YuqueError as exc:
                print(f"  ! {doc.title} 改状态失败：{exc}")

        for doc, node, v in reports:
            if v.action == "delete" or not v.new_status:
                continue
            dag = {i.doc_id: i for i in kb.toc() if i.doc_id}
            node = dag.get(doc.id)
            if node is None:
                print(f"  ! {doc.title} 找不到目录节点，跳过日志")
                continue
            try:
                kb.write_log(doc, node.uuid, v)
                print(f"  ✎ 审批日志：{doc.title} → {TIER_LABEL.get(v.tier, v.tier)}")
            except YuqueError as exc:
                print(f"  ! {doc.title} 写日志失败：{exc}")

        if archive:  # 过期周目录归档
            for wd in [i for i in items if i.type == "TITLE" and WEEK_TITLE_RE.match(i.title)]:
                m = WEEK_TITLE_RE.match(wd.title)
                end = datetime.strptime(
                    f"{kb.now.year}-{m.group(3)}-{m.group(4)}", "%Y-%m-%d"
                ).replace(tzinfo=CN, hour=23, minute=59)
                if end < kb.now:
                    kb.move(wd.uuid, archive.uuid)
                    print(f"  📦 已归档：{wd.title}")
        print("完成")
    finally:
        kb.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
