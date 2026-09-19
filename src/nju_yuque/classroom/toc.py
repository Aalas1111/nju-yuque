"""知识库**目录结构维护**（tidy）：归档区置底、归档区内部最新在上、根目录只留一个活跃目录。

⚠️ 这是唯一会**写语雀**的模块，而且只动**目录节点**（TOC）：

- 不修改任何文档正文、不新建文档、不删文档；
- 默认**不启用**（`serve`/`run` 要显式加 `--tidy-toc`，或单独跑 `yuque classroom tidy`）。

规划与执行分开：:func:`plan_tidy` 是**纯函数**（拿目录快照算出一串移动操作，离线可测），
:func:`apply_tidy` 才真的调 API。移动只依赖两个**实测可靠**的原语：

| 原语 | 实测效果 |
|---|---|
| ``appendNode`` + ``node_uuid``（不带 target） | 移到**根目录最末** |
| ``appendNode`` + ``node_uuid`` + ``target_uuid=X`` | 移到 X 的**子节点最末** |
| ``appendNode`` + ``node_uuid`` + ``target_uuid=X``（不带 target 的根级重排） | 按目标顺序依次调用即可排出任意顺序 |

（``editNode`` + ``prev_uuid`` 会静默失败，不用。）
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..models import TocItem
from . import week as week_mod

ARCHIVE_TITLE = "归档区"

OP_CREATE = "create_active"  # 新建活跃周目录
OP_UNARCHIVE = "unarchive_active"  # 活跃周目录在归档区里 → 搬回根目录
OP_ARCHIVE = "archive_folder"  # 把过期的周目录移进归档区
OP_ARCHIVE_LAST = "archive_last"  # 把归档区移到根目录最末
OP_ORDER_ARCHIVE = "order_archive"  # 归档区内部按「新→旧」从上到下


@dataclass(frozen=True)
class TidyOp:
    """一个目录移动操作。"""

    kind: str
    node_uuid: str = ""
    title: str = ""
    target_uuid: str = ""
    note: str = ""

    def describe(self) -> str:
        if self.kind == OP_CREATE:
            return f"新建活跃周目录「{self.title}」"
        if self.kind == OP_UNARCHIVE:
            return f"把「{self.title}」从归档区搬回根目录（它已是本周）"
        if self.kind == OP_ARCHIVE:
            return f"归档「{self.title}」→「{ARCHIVE_TITLE}」"
        if self.kind == OP_ARCHIVE_LAST:
            return f"把「{ARCHIVE_TITLE}」移到目录最末"
        if self.kind == OP_ORDER_ARCHIVE:
            return f"重排「{ARCHIVE_TITLE}」内部（最新在上）"
        return f"{self.kind} {self.title}"


@dataclass
class TidyPlan:
    """一次整理要做的事。"""

    ops: list[TidyOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    active_title: str = ""
    archived: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.ops

    def describe(self) -> str:
        if self.empty:
            return "目录已经是目标形态，无需整理"
        return f"{len(self.ops)} 个操作：" + "；".join(op.describe() for op in self.ops)


def _root_children(items: Iterable[TocItem]) -> list[TocItem]:
    return [i for i in items if not i.parent_uuid]


def plan_tidy(
    items: list[TocItem],
    *,
    today: date,
    unsettled: dict[str, int] | None = None,
    archive_title: str = ARCHIVE_TITLE,
) -> TidyPlan:
    """算出该做哪些目录调整（**纯函数，不联网**）。

    目标形态：

    1. 根目录下**只有一个** `MMDD-MMDD` 周目录（= 今天所在的那一周）；
    2. 「归档区」在根目录**最末**；
    3. 「归档区」内部：周目录按**时间从晚到早**自上而下；非周目录的节点不参与排序（被挤到前面）。

    ``unsettled`` 是「目录名 → 还有几份已受理但没结案的申请」，只用来**告警**，不阻止归档。
    """
    unsettled = unsettled or {}
    plan = TidyPlan(active_title=week_mod.week_of(today).title)
    root = _root_children(items)
    titles = {i.title: i for i in root if i.type == "TITLE"}

    archive = titles.get(archive_title)
    active = titles.get(plan.active_title)
    # 活跃周目录可能在归档区里（上周建的「下周目录」到点就该被重新启用）
    active_anywhere = next(
        (i for i in items if i.type == "TITLE" and i.title == plan.active_title), None
    )

    # 0) 冲突检查：根目录里有两个同名分组时不动手（避免瞎猜哪个是归档区）
    dupes = [i.title for i in root if i.type == "TITLE"]
    if dupes.count(archive_title) > 1 or dupes.count(plan.active_title) > 1:
        plan.warnings.append(f"根目录下有重名分组，先人工处理：{dupes}")
        return plan

    # 1) 归档区不存在 → 交给调用方先建（这里只提醒）
    if archive is None:
        plan.notes.append(f"根目录还没有「{archive_title}」，请先建一个（tidy 不会凭空建分组）")

    # 2) 活跃周目录不存在 → 新建；在归档区里 → 搬回根目录（不能重复建一个）
    if active is None and active_anywhere is not None:
        plan.ops.append(
            TidyOp(OP_UNARCHIVE, node_uuid=active_anywhere.uuid, title=plan.active_title)
        )
        plan.notes.append(f"「{plan.active_title}」已在归档区里，本周轮到它 → 搬回根目录")
    elif active is None:
        plan.ops.append(TidyOp(OP_CREATE, title=plan.active_title))
        plan.notes.append(f"根目录没有本周目录，将新建「{plan.active_title}」")

    # 3) 过期的周目录 → 移进归档区
    if archive is not None:
        for item in root:
            if item.type != "TITLE" or item.title == archive_title:
                continue
            if item.uuid == (active_anywhere.uuid if active_anywhere else ""):
                continue
            if not week_mod.is_week_title(item.title):
                continue  # 不是周目录就不碰
            plan.ops.append(
                TidyOp(OP_ARCHIVE, node_uuid=item.uuid, title=item.title, target_uuid=archive.uuid)
            )
            plan.archived.append(item.title)
            pending = unsettled.get(item.title, 0)
            if pending:
                plan.warnings.append(
                    f"「{item.title}」还有 {pending} 份已受理但未结案的申请，归档后 agent 不再处理它"
                )

        # 4) 归档区内部：周目录按「新→旧」重排（依次挪到末尾）
        #    要把本轮**即将归档进来的**也算上，否则刚归档的那个会跑到最下面。
        children = [i for i in items if i.parent_uuid == archive.uuid]
        weeks = [i for i in children if i.type == "TITLE" and week_mod.is_week_title(i.title)]
        leaving = {op.node_uuid for op in plan.ops if op.kind == OP_UNARCHIVE}
        incoming = [
            i
            for i in root
            if i.type == "TITLE" and week_mod.is_week_title(i.title) and i.title in plan.archived
        ]
        resulting = [i for i in [*weeks, *incoming] if i.uuid not in leaving]
        wanted = [
            i
            for i, _wk in sorted(
                ((i, week_mod.parse_week_title(i.title, today=today)) for i in resulting),
                key=lambda pair: pair[1].start if pair[1] else date.min,
                reverse=True,
            )
        ]
        if [i.uuid for i in wanted] != [i.uuid for i in weeks]:
            plan.ops.append(
                TidyOp(
                    OP_ORDER_ARCHIVE, node_uuid=archive.uuid, note="→".join(i.title for i in wanted)
                )
            )

        # 5) 归档区必须在根目录最末
        #    注意：unarchive / create 都是「往根目录末尾追加」，会让归档区不再是最后一个，
        #    所以只要本轮有这类操作，就得重新把归档区推到最末。
        appended_to_root = any(op.kind in (OP_CREATE, OP_UNARCHIVE) for op in plan.ops)
        if root and (root[-1].uuid != archive.uuid or appended_to_root):
            plan.ops.append(TidyOp(OP_ARCHIVE_LAST, node_uuid=archive.uuid))

    return plan


def apply_tidy(api: Any, repo: str, plan: TidyPlan, *, log: Any = print) -> list[str]:
    """执行整理计划，返回做过的事情（人话）。"""
    done: list[str] = []
    archive_uuid = ""
    for op in plan.ops:
        if op.kind == OP_CREATE:
            created = api.toc_add(repo, title=op.title, node_type="TITLE")
            archive_uuid = archive_uuid or ""
            done.append(op.describe())
            log(f"[tidy] ✓ {op.describe()}" + (f"（{created}）" if created else ""))
        elif op.kind == OP_UNARCHIVE:
            api.toc_place(repo, node_uuid=op.node_uuid)  # 不带 target = 移到根目录末尾
            done.append(op.describe())
            log(f"[tidy] ✓ {op.describe()}")
        elif op.kind == OP_ARCHIVE:
            api.toc_place(repo, node_uuid=op.node_uuid, target_uuid=op.target_uuid)
            done.append(op.describe())
            log(f"[tidy] ✓ {op.describe()}")
        elif op.kind == OP_ARCHIVE_LAST:
            api.toc_place(repo, node_uuid=op.node_uuid)
            done.append(op.describe())
            log(f"[tidy] ✓ {op.describe()}")
        elif op.kind == OP_ORDER_ARCHIVE:
            children = [i for i in api.toc(repo) if i.parent_uuid == op.node_uuid]
            weeks = [i for i in children if i.type == "TITLE" and week_mod.is_week_title(i.title)]
            wanted = sorted(
                weeks,
                key=lambda i: (
                    week_mod.parse_week_title(i.title, today=date.today()).start
                    if week_mod.parse_week_title(i.title, today=date.today())
                    else date.min
                ),
                reverse=True,
            )
            for item in wanted:  # 依次挪到末尾 → 最终「最新在最上」
                api.toc_place(repo, node_uuid=item.uuid, target_uuid=op.node_uuid)
            done.append(op.describe())
            log(f"[tidy] ✓ {op.describe()}")
    for warning in plan.warnings:
        log(f"[tidy] ⚠️ {warning}")
    return done
