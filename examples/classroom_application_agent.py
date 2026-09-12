"""教室借用申请 · 知识库自动维护 agent（单库版）

做四件事：

1. **审核**：读取申请文档，按《指导文档》的规则校验（状态/必填/48h/节次/人数）；
2. **整理结构**：文档放错位置但能判断归属 → 移动到对应的周目录；无法识别 → 删除；
3. **审批日志**：在申请文档下维护一份子文档 `审批日志`，追加时间戳 + 结论 + 意见；
4. **归档**：把过期的周目录移动到 `99-归档`。

默认 **dry-run**（只打印计划，不动数据）；加 `--apply` 才真正执行。

用法::

    export YUQUE_HOME=~/.yuque           # 或任一已登录的目录
    uv run python examples/classroom_application_agent.py --repo lqogh0/jsjysq
    uv run python examples/classroom_application_agent.py --repo lqogh0/jsjysq --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from nju_yuque.api import YuqueApi
from nju_yuque.errors import YuqueError
from nju_yuque.session import Credentials

# ---------------------------------------------------------------- 规则常量
GUIDE_TITLE = "00-指导文档（必读）"
TEMPLATE_TITLE = "教室申请模板（复制后填写）"
LOG_TITLE = "审批日志"  # 申请文档下由 agent 维护的子文档
ARCHIVE_TITLE = "归档区"
# 周目录命名：0914-0920（MMDD-MMDD），后面允许跟任意说明文字
WEEK_TITLE_RE = re.compile(r"^(\d{2})(\d{2})\s*[-~～]\s*(\d{2})(\d{2})")

# 状态机（agent 会改写文档首行的「状态」）
STATUS_PENDING = "待提交"  # 待处理
STATUS_REGISTERED = "已登记（等待提交教室申请）"  # 校验通过
STATUS_REJECTED = "已退回（修改后请把状态改为待提交）"  # 校验不通过
STATUS_RE = re.compile(r"^([ 	>*\-•]*\**\s*状态\s*\**\s*[:：][ 	]*)(.*)$", re.MULTILINE)

ADVANCE_HOURS = 48  # 必须提前 48 小时

PERIOD_START = {
    1: "08:00",
    2: "09:00",
    3: "10:10",
    4: "11:10",
    5: "14:00",
    6: "15:00",
    7: "16:10",
    8: "17:10",
    9: "18:30",
    10: "19:30",
    11: "20:30",
    12: "21:30",
}

REQUIRED_FIELDS = (
    "活动名称",
    "申请人",
    "真实姓名",
    "联系方式",
    "活动日期",
    "使用节次",
    "预计人数",
    "意向教室或教学楼",
)

CN = timezone(timedelta(hours=8))

FIELD_RE = re.compile(
    # 注意：[ \t] 而不是 \s —— \s 会吃掉换行，导致空值字段把下一行当成自己的值
    r"^[ \t>*\-•]*\**[ \t]*(状态|活动名称|申请人|真实姓名|联系方式|活动日期|使用节次|"
    r"预计人数|意向教室或教学楼|活动简介)[ \t]*\**[ \t]*[:：][ \t]*(.*?)[ \t]*$",
    re.MULTILINE,
)


# ---------------------------------------------------------------- 数据结构
@dataclass
class Verdict:
    action: str  # keep / move / delete / skip
    target_title: str = ""
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    note: str = ""
    new_status: str = ""  # agent 要写回文档首行的新状态


# ---------------------------------------------------------------- 写回状态
def set_status(body: str, new_status: str) -> str:
    """把文档首行的「状态：xxx」改成新状态；没有这一行就补在开头。"""
    if STATUS_RE.search(body or ""):
        return STATUS_RE.sub(lambda m: m.group(1) + new_status, body, count=1)
    return f"状态：{new_status}\n\n{body or ''}"


# ---------------------------------------------------------------- 解析
def parse_fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in FIELD_RE.finditer(body or ""):
        key, value = m.group(1), m.group(2).strip()
        # 去掉 markdown 强调符号与占位符
        value = value.strip("*_` ").strip()
        if value in {"", "-", "—", "（必填）", "(必填)"}:
            value = ""
        out.setdefault(key, value)  # 同名取第一次出现
    return out


def looks_like_template(fields: dict[str, str]) -> bool:
    return len(fields) >= 4


def parse_date(raw: str, now: datetime) -> datetime | None:
    """支持 2026-09-16 / 2026-09-16（周三） / 2026/9/16 / 09-16。"""
    text = raw.strip()
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"^(\d{1,2})[-/月](\d{1,2})", text)
        if not m:
            return None
        y, mo, d = now.year, int(m.group(1)), int(m.group(2))
    try:
        dt = datetime(y, mo, d, tzinfo=CN)
    except ValueError:
        return None
    # 只写了月日且已过去很久 → 视作明年
    if "年" not in text and dt.date() < (now - timedelta(days=180)).date():
        try:
            dt = dt.replace(year=y + 1)
        except ValueError:
            return None
    return dt


def parse_period(raw: str) -> tuple[int, int] | None:
    text = raw.strip()
    m = re.search(r"(\d{1,2})\s*[-~～至到]\s*(\d{1,2})", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})", text)
        if not m:
            return None
        a = b = int(m.group(1))
    if not (1 <= a <= 12 and 1 <= b <= 12 and a <= b):
        return None
    return a, b


def parse_people(raw: str) -> int | None:
    m = re.search(r"(\d+)", raw)
    if not m:
        return None
    n = int(m.group(1))
    return n if n > 0 else None


def evaluate(fields: dict[str, str], now: datetime) -> Verdict:
    problems: list[str] = []
    status = fields.get("状态", "")
    if status != STATUS_PENDING:
        return Verdict(
            action="keep",
            problems=[],
            fields=fields,
            note=f"状态为「{status or '缺失'}」，跳过（只处理「待提交」）",
        )

    for name in REQUIRED_FIELDS:
        if not fields.get(name):
            problems.append(f"「{name}」为空")

    date = parse_date(fields.get("活动日期", ""), now)
    period = parse_period(fields.get("使用节次", ""))
    people = parse_people(fields.get("预计人数", ""))

    if fields.get("活动日期") and date is None:
        problems.append(f"「活动日期」无法解析：{fields['活动日期']}")
    if fields.get("使用节次") and period is None:
        problems.append(f"「使用节次」不合法（应为 1~12 或 起-止）：{fields['使用节次']}")
    if fields.get("预计人数") and people is None:
        problems.append(f"「预计人数」不是正整数：{fields['预计人数']}")

    start = None
    if date and period:
        hh, mm = (int(x) for x in PERIOD_START[period[0]].split(":"))
        start = date.replace(hour=hh, minute=mm)
        gap = start - now
        if gap < timedelta(hours=ADVANCE_HOURS):
            hours = gap.total_seconds() / 3600
            problems.append(
                f"活动开始时间 {start:%Y-%m-%d %H:%M} 距今仅 {hours:.1f} 小时，不足 {ADVANCE_HOURS} 小时"
            )

    return Verdict(
        action="keep",
        ok=not problems,
        problems=problems,
        fields=fields,
        target_title=start.strftime("%Y-%m-%d") if start else "",
    )


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

    # -- 目录 ---------------------------------------------------------
    def toc(self) -> list:
        return self.api.toc(self.repo)

    def node_path(self, items: list) -> dict[str, list[str]]:
        by_uuid = {i.uuid: i for i in items}
        out: dict[str, list[str]] = {}
        for item in items:
            chain, parent = [], item.parent_uuid
            while parent and parent in by_uuid:
                chain.append(by_uuid[parent].title)
                parent = by_uuid[parent].parent_uuid
            out[item.uuid] = list(reversed(chain))
        return out

    def docs(self) -> list:
        return self.api.docs(self.repo)

    def read(self, doc_id: int) -> str:
        d = self.api.doc(self.repo, str(doc_id))
        return d.body or ""

    # -- 写 -----------------------------------------------------------
    def move(self, node_uuid: str, parent_uuid: str) -> None:
        self.api.toc_move(self.repo, node_uuid=node_uuid, target_uuid=parent_uuid)

    def mount(self, doc_id: int, parent_uuid: str) -> None:
        self.api.toc_add(self.repo, doc_ids=[doc_id], target_uuid=parent_uuid)

    def delete(self, doc_id: int) -> None:
        self.api.delete_doc(self.repo, doc_id)

    def append_log(self, app_doc, log_doc, parent_node_uuid: str, verdict: Verdict) -> str:
        """在申请文档下维护/追加「审批日志」。返回写进去的正文片段。"""
        stamp = self.now.strftime("%Y-%m-%d %H:%M")
        if verdict.action == "delete":
            return ""
        if verdict.ok:
            head = f"### {stamp} · 校验通过\n"
            lines = [
                "- 结论：**通过**",
                f"- 活动日期：{verdict.fields.get('活动日期', '')}",
                f"- 使用节次：{verdict.fields.get('使用节次', '')}",
                f"- 预计人数：{verdict.fields.get('预计人数', '')}",
                "- 处理：已登记，等待批量提交",
            ]
        else:
            head = f"### {stamp} · 退回修改\n"
            lines = (
                ["- 结论：**退回修改**", "- 原因："]
                + [f"  {i}. {p}" for i, p in enumerate(verdict.problems, 1)]
                + ["- 处理：请修改后把「状态」保持不变（待提交），agent 会在下一轮重新校验"]
            )

        section = head + "\n".join(lines) + "\n"
        if log_doc is None:
            body = (
                f"> ⚙️ 本文件由系统自动维护，请勿手动编辑。\n\n"
                f"# 审批日志 · {app_doc.title}\n\n{section}"
            )
            created = self.api.create_doc(self.repo, title=LOG_TITLE, body=body)
            self.mount(created.id, parent_node_uuid)
            return section
        body = self.read(log_doc.id) + "\n" + section
        self.api.update_doc(self.repo, log_doc.id, body=body)
        return section


# ---------------------------------------------------------------- 主流程
def _week_dir_for(week_dirs: list, date: datetime | None, now: datetime):
    """日期落在哪个周目录里。"""
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


def plan(kb: Kb) -> list[tuple]:
    items = kb.toc()
    path = kb.node_path(items)
    doc_node = {i.doc_id: i for i in items if i.doc_id}
    week_dirs = [i for i in items if i.type == "TITLE" and WEEK_TITLE_RE.match(i.title)]
    structural = {GUIDE_TITLE, TEMPLATE_TITLE, ARCHIVE_TITLE, LOG_TITLE}

    out = []
    for doc in kb.docs():
        node = doc_node.get(doc.id)
        in_archive = bool(node) and ARCHIVE_TITLE in path.get(node.uuid, [])

        if doc.title in structural:
            out.append((doc, node, Verdict("skip", note="结构性文档")))
            continue

        fields = parse_fields(kb.read(doc.id))
        if not looks_like_template(fields):
            out.append(
                (
                    doc,
                    node,
                    Verdict(
                        "delete", note=f"未使用模板（仅识别到 {len(fields)} 个字段），无法判断归属"
                    ),
                )
            )
            continue

        v = evaluate(fields, kb.now)
        if v.fields.get("状态") != "待提交":
            out.append((doc, node, v))
            continue

        v.new_status = STATUS_REGISTERED if v.ok else STATUS_REJECTED

        date = parse_date(v.fields.get("活动日期", ""), kb.now)
        target = _week_dir_for(week_dirs, date, kb.now)
        if target is None:
            v.note = "找不到对应的周目录（日期超出当前窗口），保持原位并记录"
        elif in_archive or node is None or node.parent_uuid != target.uuid:
            v.action = "move"
            v.target_title = target.title
        out.append((doc, node, v))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="教室申请知识库自动维护（默认 dry-run）")
    ap.add_argument("--repo", required=True, help="知识库 id 或 group/slug")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    args = ap.parse_args()

    kb = Kb(args.repo)
    try:
        items = kb.toc()
        path = kb.node_path(items)
        archive = next((i for i in items if i.type == "TITLE" and i.title == ARCHIVE_TITLE), None)

        print(f"# KB={args.repo}  现在={kb.now:%Y-%m-%d %H:%M} (UTC+8)  dry_run={not args.apply}\n")
        reports = plan(kb)
        for doc, node, v in reports:
            loc = " / ".join(path.get(node.uuid, [])) if node else "(不在目录)"
            print(f"## {doc.title}  (#{doc.id})")
            print(f"   位置: {loc or '(根)'}   节点: {node.uuid if node else '无'}")
            print(f"   判定: {v.action} {('→ ' + v.target_title) if v.target_title else ''}")
            if v.new_status:
                print(f"   状态: 待提交 → {v.new_status}")
            if v.note:
                print(f"   说明: {v.note}")
            for p in v.problems:
                print(f"   ✗ {p}")
            if v.fields:
                print(f"   字段: { {k: val for k, val in v.fields.items() if val} }")
            print()

        if not args.apply:
            print("（dry-run，未做任何修改；加 --apply 执行）")
            return

        print("=" * 60, "\n开始执行")
        for doc, node, v in reports:
            try:
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
            except YuqueError as exc:
                print(f"  ! {doc.title} 操作失败：{exc}")

        # 写回状态（移动之后再写，避免中途失败导致状态已改但位置没动）
        for doc, _node, v in reports:
            if not v.new_status:
                continue
            try:
                kb.api.update_doc(kb.repo, doc.id, body=set_status(kb.read(doc.id), v.new_status))
                print(f"  ⇄ 状态已改为：{doc.title} → {v.new_status}")
            except YuqueError as exc:
                print(f"  ! {doc.title} 改状态失败：{exc}")

        # 审批日志（移动后再写，确保拿得到父节点）
        for doc, _node, v in reports:
            if v.action == "delete" or not v.new_status:
                continue
            try:
                fresh = {i.doc_id: i for i in kb.toc() if i.doc_id}
                parent_node = fresh.get(doc.id)
                if parent_node is None:
                    print(f"  ! {doc.title} 找不到目录节点，跳过日志")
                    continue
                log_doc = next(
                    (
                        d
                        for d in kb.docs()
                        if d.title == LOG_TITLE
                        and any(
                            i.doc_id == d.id and i.parent_uuid == parent_node.uuid for i in kb.toc()
                        )
                    ),
                    None,
                )
                kb.append_log(doc, log_doc, parent_node.uuid, v)
                print(f"  ✎ 已写审批日志：{doc.title} → {'通过' if v.ok else '退回修改'}")
            except YuqueError as exc:
                print(f"  ! {doc.title} 写日志失败：{exc}")

        # 归档：过期的周目录
        if archive:
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
