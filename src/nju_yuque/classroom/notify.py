"""通知层：agent → qqbot 的**单向事件流**。

agent 只负责「产生事件」，不负责「投递到 QQ」。中间用**文件 outbox** 解耦：

```
<outdir>/notify/
├── 0921-0927/                                            ← 这一周
│   ├── pending/000012-rejected-1234567-9f2c1a0b7e34.json ← 待消费（qqbot 读它）
│   ├── done/000012-rejected-1234567-9f2c1a0b7e34.json    ← 消费完挪进来（或删掉）
│   └── outbox.jsonl                                      ← 本周只追加的审计流水
└── 0914-0920/                                            ← 上周的（历史归档）
```

**按周分组**：一周一个目录；已投递的都在各自的 ``done/`` 里，所以「归档」天然就有。
事件落在**自己 created_at 所属的那一周**（不是「当前周」），跨周那一刻写入的事件不会串周。

消费约定（给 qqbot 的实现者）：

1. **扫所有周的** ``<outdir>/notify/*/pending/*.json``：先按周目录名、再按文件名前缀 ``seq``
   从小到大处理（这样上周没投递完的事件也不会漏）；
2. 处理完把文件**移动**到**同一个周目录下的** ``done/``（或删除）——移动成功即视为已投递；
3. 文件名里的 ``seq`` 只用于排序，不要求连续；
4. 也可以直接读各周的 ``outbox.jsonl`` 并自行记录 offset（适合流式消费），但**不要**既读 JSONL
   又挪文件，否则会重复投递。

每个事件的字段见 :class:`Notice`；``message`` 是已经渲染好的中文正文（可直接发），
``reasons`` / ``warnings`` 是结构化版本（想自己排版就用它）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contract import SCHEMA_VERSION, iso

# ---------------------------------------------------------------- 事件种类
KIND_ACCEPTED = "accepted"  # 要素齐备，已产出申请 JSON
KIND_REJECTED = "rejected"  # 要素不对，请修改文档
KIND_UNRECOGNIZED = "unrecognized"  # 看不出是申请
KIND_TAMPERED = "tampered"  # 已提交的文档被改动（修改无效）
KIND_DELETED_SUBMITTED = "deleted_submitted"  # 已提交的文档被删除（不允许撤回）
KIND_DELETED_REJECTED = "deleted_rejected"  # 被退回 / 未识别的文档被删除

NOTICE_KINDS = (
    KIND_ACCEPTED,
    KIND_REJECTED,
    KIND_UNRECOGNIZED,
    KIND_TAMPERED,
    KIND_DELETED_SUBMITTED,
    KIND_DELETED_REJECTED,
)
DEFAULT_KINDS: tuple[str, ...] = NOTICE_KINDS

# 周目录名（MMDD-MMDD），用来区分「周目录」和别的目录
WEEK_DIR_RE = re.compile(r"^\d{4}-\d{4}$")


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class NoticeDoc(_Base):
    """事件相关的语雀文档。"""

    repo: str = ""
    doc_id: int = 0
    slug: str = ""
    title: str = ""
    url: str = ""


class NoticeMember(_Base):
    """要通知的人。qqbot 需要自己把语雀身份映射到 QQ 号（agent 不做这个映射）。"""

    yuque_id: int = 0
    yuque_login: str = ""
    name: str = ""
    applicant_raw: str = Field(default="", description="文档里填写的「申请人」原值")


class Notice(_Base):
    """一条待投递的通知事件。"""

    schema_version: str = SCHEMA_VERSION
    seq: int = Field(description="单调递增序号，用于排队与去重")
    notice_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = ""
    kind: str = Field(description="事件类型：" + " / ".join(NOTICE_KINDS))
    repo: str = ""
    doc: NoticeDoc = Field(default_factory=NoticeDoc)
    member: NoticeMember = Field(default_factory=NoticeMember)
    summary: str = Field(default="", description="一句话摘要（适合直接当消息标题）")
    message: str = Field(default="", description="渲染好的中文正文，可直接发")
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    application_id: str = ""
    application_file: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------- 文案
def render(
    kind: str,
    *,
    title: str,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    date: str = "",
    period: str = "",
    campus: str = "",
    doc_url: str = "",
) -> tuple[str, str]:
    """返回 ``(summary, message)``。文案集中在这里，方便统一调整语气。"""
    reasons = list(reasons or [])
    warnings = list(warnings or [])
    where = " ".join(x for x in (date, period and f"第 {period} 节", campus) if x)
    link = f"\n文档：{doc_url}" if doc_url else ""
    tail = "（本文档由 agent 自动识别，无需回复本消息）"

    if kind == KIND_ACCEPTED:
        summary = f"「{title}」已受理，正在排队提交教室借用申请"
        lines = [f"✅ 「{title}」要素齐备，已生成教室借用申请。", f"时间：{where}"]
        if warnings:
            lines += ["", "⚠️ 请确认："] + [f"- {w}" for w in warnings]
        lines += ["", "如信息有误请尽快联系负责人；本文档已锁定，不能再修改。", link, tail]
        return summary, "\n".join(x for x in lines if x != "\n").strip()

    if kind == KIND_REJECTED:
        summary = f"「{title}」未通过校验，请修改后我会自动重审"
        lines = [f"❌ 「{title}」这份申请我没法处理，原因："]
        lines += [f"{i}. {r}" for i, r in enumerate(reasons, 1)]
        if warnings:
            lines += ["", "⚠️ 另外提醒："] + [f"- {w}" for w in warnings]
        lines += ["", "改完把文档保存一下就行（不用做别的动作），我会重新校验。", link, tail]
        return summary, "\n".join(lines).strip()

    if kind == KIND_UNRECOGNIZED:
        summary = f"「{title}」看不出是教室借用申请"
        lines = [f"🤔 「{title}」这篇文档我没法识别成教室借用申请。"]
        lines += [f"{i}. {r}" for i, r in enumerate(reasons, 1)]
        lines += [
            "",
            "请按知识库模板新建文档填写（至少要能看出「哪一天、几点到几点、哪个校区」）。",
            link,
            tail,
        ]
        return summary, "\n".join(lines).strip()

    if kind == KIND_TAMPERED:
        summary = f"「{title}」的申请已提交，修改无效"
        lines = [
            f"🔒 「{title}」对应的申请已经提交，在这份文档里修改是**无效的**。",
            "改动**不会**同步到已提交的申请，请不要在这里改。",
        ]
        if reasons:
            lines += ["", "本次改动的字段："] + [f"- {r}" for r in reasons]
        lines += [
            "",
            "如果确实要改申请内容，请联系负责人处理，或在语雀里新建一份申请。",
            link,
            tail,
        ]
        return summary, "\n".join(lines).strip()

    if kind == KIND_DELETED_SUBMITTED:
        summary = f"「{title}」的申请文档被删除了，但申请不允许撤回"
        lines = [
            f"⚠️ 「{title}」对应的申请文档已经被删除。",
            "申请已经在流程里，**删掉语雀文档不会撤回申请**。",
            "如果确实需要撤回或修改，请联系负责人。",
            link,
            tail,
        ]
        return summary, "\n".join(lines).strip()

    if kind == KIND_DELETED_REJECTED:
        summary = f"「{title}」这份申请文档已被删除"
        lines = [
            f"🗑️ 「{title}」这份申请文档已经被删除（之前的状态是「需要修改」）。",
            "如果你本来就打算重新写一份，这条消息忽略即可。",
            link,
            tail,
        ]
        return summary, "\n".join(lines).strip()

    return f"[{kind}] {title}", "\n".join([f"{kind}: {title}", *reasons]).strip()


# ---------------------------------------------------------------- 接口
class Notifier(Protocol):
    """通知出口。实现者只需要把 :class:`Notice` 送出去，不要改它的语义。"""

    def send(self, notice: Notice) -> None: ...


class NullNotifier:
    """什么都不做（dry-run / 单测用）。"""

    def send(self, notice: Notice) -> None:  # noqa: ARG002
        return None


class ConsoleNotifier:
    """打到 stdout，方便本地观察与排错。"""

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout

    def send(self, notice: Notice) -> None:
        self.stream.write(f"\n--- notice #{notice.seq} [{notice.kind}] ---\n{notice.message}\n")
        self.stream.flush()


class FileOutboxNotifier:
    """文件 outbox：``<周>/pending/`` 一份事件一个文件 + 每周一份 ``outbox.jsonl`` 审计流水。

    事件按 **created_at 所属的那一周** 落盘（不是「当前周」），跨周时不会串。
    ``list_pending()`` 扫**所有周**，所以上周没投递完的事件仍然会被投出去。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    # -- 位置计算 ---------------------------------------------------------
    def week_dir(self, when: str | datetime | None = None) -> Path:
        """某个时间点属于哪一周的目录（默认今天）。"""
        from .week import week_of

        day: date
        if isinstance(when, datetime):
            day = when.date()
        elif when:
            try:
                day = datetime.fromisoformat(str(when)).date()
            except ValueError:
                day = datetime.now().date()
        else:
            day = datetime.now().date()
        return self.root / week_of(day).title

    def dirs_for(self, when: str | datetime | None = None) -> tuple[Path, Path, Path]:
        """(pending 目录, done 目录, 审计流水文件)。"""
        week = self.week_dir(when)
        return week / "pending", week / "done", week / "outbox.jsonl"

    def notice_path(self, notice: Notice) -> Path:
        """``<周>/pending/<seq>-<kind>-<doc_id>-<notice_id>.json``。

        带上 ``notice_id`` 是为了幂等：state.json 被重置后 seq 会从 1 重来，
        只靠 seq 会**静默覆盖**还没投递出去的通知（qqbot 永远收不到）。
        消费方只应按 ``seq`` 排序、不要解析其余部分。
        """
        pending, _done, _log = self.dirs_for(notice.created_at)
        return (
            pending / f"{notice.seq:06d}-{notice.kind}-{notice.doc.doc_id}-{notice.notice_id}.json"
        )

    def send(self, notice: Notice) -> None:
        target = self.notice_path(notice)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(notice.to_json(), encoding="utf-8")
        os.replace(tmp, target)
        with self.dirs_for(notice.created_at)[2].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(notice.model_dump(mode="json"), ensure_ascii=False) + "\n")

    # -- 消费侧工具（qqbot 也可以自己实现）------------------------------
    def week_dirs(self) -> list[Path]:
        """所有周目录（按名字排序 = 按时间排序）。"""
        if not self.root.exists():
            return []
        return sorted(d for d in self.root.iterdir() if d.is_dir() and WEEK_DIR_RE.match(d.name))

    def pending_dirs(self) -> list[Path]:
        """所有「待投递」目录：每周一个 ``<周>/pending``（外加迁移期的扁平 ``pending/``）。"""
        dirs: list[Path] = []
        legacy = self.root / "pending"  # 老扁平布局（migrate 之后就没有了）
        if legacy.exists():
            dirs.append(legacy)
        dirs += [d / "pending" for d in self.week_dirs()]
        return [d for d in dirs if d.exists()]

    def list_pending(self) -> list[Path]:
        """**所有周**里还没投递的事件（先按周、再按 seq 排序）。"""
        out: list[Path] = []
        for directory in self.pending_dirs():
            out += sorted(
                (p for p in directory.glob("*.json") if p.is_file()), key=lambda p: p.name
            )
        return out

    def ack(self, seq: int) -> Path | None:
        """把序号为 ``seq`` 的事件挪到**同一个周目录下**的 ``done/``（没有则 None）。"""
        for path in self.list_pending():
            if path.name.startswith(f"{seq:06d}-"):
                done = path.parent.parent / "done"
                done.mkdir(parents=True, exist_ok=True)
                target = done / path.name
                os.replace(path, target)
                return target
        return None

    def ack_all(self) -> list[Path]:
        """按序号顺序把当前所有待投递事件挪到各自周目录的 ``done/``。"""
        done: list[Path] = []
        for seq in sorted(self._pending_seqs()):
            moved = self.ack(seq)
            if moved is not None:
                done.append(moved)
        return done

    def _pending_seqs(self) -> list[int]:
        seqs = []
        for path in self.list_pending():
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                seqs.append(int(head))
        return seqs

    def migrate_flat_layout(self) -> list[str]:
        """把旧的扁平布局（``notify/pending|done|outbox.jsonl``）搬进按周目录。

        按每条事件自己的 ``created_at`` 归周；认不出来就归到「今天那一周」。幂等。
        """
        moved: list[str] = []
        for source, is_done in ((self.root / "pending", False), (self.root / "done", True)):
            if not source.exists():
                continue
            for path in sorted(source.glob("*.json")):
                try:
                    created = json.loads(path.read_text(encoding="utf-8")).get("created_at", "")
                except (OSError, ValueError):
                    created = ""
                pending, done, _log = self.dirs_for(created)
                target_dir = done if is_done else pending
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / path.name
                if target.exists():
                    path.unlink()
                else:
                    os.replace(path, target)
                moved.append(str(target))
            try:
                source.rmdir()
            except OSError:
                pass
        legacy_log = self.root / "outbox.jsonl"
        if legacy_log.exists():
            for line in legacy_log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    created = json.loads(line).get("created_at", "")
                except ValueError:
                    created = ""
                _pending, _done, log_file = self.dirs_for(created)
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            legacy_log.unlink()
            moved.append(str(legacy_log))
        return moved

    def purge_done(self) -> int:
        """清掉所有周目录里 ``done/`` 下的文件（审计流水 ``outbox.jsonl`` 保留）。"""
        count = 0
        for directory in [self.root / "done"] + [d / "done" for d in self.week_dirs()]:
            if not directory.exists():
                continue
            for item in directory.glob("*.json"):
                item.unlink()
                count += 1
        return count


class CompositeNotifier:
    """把事件同时发给多个出口（例如 outbox + 控制台）。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, notice: Notice) -> None:
        for notifier in self.notifiers:
            notifier.send(notice)


def build_notifier(spec: str, outdir: Path | str) -> Notifier:
    """按 ``spec`` 组装通知出口：``outbox`` / ``console`` / ``none``，逗号分隔。"""
    names = [x.strip() for x in (spec or "outbox").split(",") if x.strip()]
    out: list[Notifier] = []
    for name in names:
        if name == "outbox":
            out.append(FileOutboxNotifier(Path(outdir) / "notify"))
        elif name == "console":
            out.append(ConsoleNotifier())
        elif name == "none":
            out.append(NullNotifier())
        else:
            raise ValueError(f"未知的通知出口：{name}（可选 outbox / console / none）")
    return CompositeNotifier(out) if len(out) > 1 else (out[0] if out else NullNotifier())


def make_notice(
    *,
    seq: int,
    kind: str,
    now: datetime,
    repo: str,
    doc: NoticeDoc,
    member: NoticeMember,
    application_id: str = "",
    application_file: str = "",
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    date: str = "",
    period: str = "",
    campus: str = "",
    extra: dict[str, Any] | None = None,
) -> Notice:
    """按事件类型渲染好文案，造一条 :class:`Notice`。"""
    summary, message = render(
        kind,
        title=doc.title or f"#{doc.doc_id}",
        reasons=reasons,
        warnings=warnings,
        date=date,
        period=period,
        campus=campus,
        doc_url=doc.url,
    )
    return Notice(
        seq=seq,
        created_at=iso(now),
        kind=kind,
        repo=repo,
        doc=doc,
        member=member,
        summary=summary,
        message=message,
        reasons=list(reasons or []),
        warnings=list(warnings or []),
        application_id=application_id,
        application_file=application_file,
        extra=extra or {},
    )
