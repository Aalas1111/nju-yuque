"""一轮处理的编排：**读知识库 → 比对本地状态 → 判定 → 产出申请 JSON / 通知 → 落盘**。

这里是把 :mod:`rules`（判定）、:mod:`store`（持久化）、:mod:`notify`（通知）缝起来的
唯一地方，也是**唯一会写文件**的地方。三类副作用：

1. ``<outdir>/applications/<application_id>.json``：要素齐备的申请（交接给提交方）；
2. ``<outdir>/notify/pending/*.json`` + ``outbox.jsonl``：给 qqbot 的事件流；
3. ``<outdir>/state.json``：本地状态（不写回语雀）。

**agent 对语雀只读**：不建文档、不改内容、不写状态、不移动目录、不删文档。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..errors import NotFoundError, YuqueError
from ..models import Doc, TocItem
from . import notify as notify_mod
from . import rules
from . import week as week_mod
from .contract import (
    SCHEMA_VERSION,
    Activity,
    Application,
    SourceRef,
    dumps,
    iso,
    make_application_id,
)
from .notify import (
    KIND_ACCEPTED,
    KIND_DELETED_REJECTED,
    KIND_DELETED_SUBMITTED,
    KIND_REJECTED,
    KIND_TAMPERED,
    KIND_UNRECOGNIZED,
    NOTICE_KINDS,
    Notice,
    NoticeDoc,
    NoticeMember,
    Notifier,
)
from .store import (
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_UNRECOGNIZED,
    DocState,
    Store,
)

CN = rules.CN

# ---------------------------------------------------------------- 结构性约定
ARCHIVE_TITLE = "归档区"  # 这个目录（及其子树）是终点站：里面的文档一律不处理
LEGACY_LOG_TITLE = "审批日志"  # 上一版设计留的子文档，现在由 qqbot 通知取代
GUIDE_HINTS = ("指导文档", "填表说明")  # 只在「首次自动识别指导文档」时用一次，之后按 id 锁定


# ---------------------------------------------------------------- 数据源
class Source(Protocol):
    """agent 需要的语雀读取能力（只读）。测试里用假实现替换。"""

    def toc(self) -> list[TocItem]: ...
    def docs(self) -> list[Doc]: ...
    def read(self, doc_id: int) -> Doc: ...


class ApiSource:
    """官方 OpenAPI 实现（只读，令牌模式）。"""

    def __init__(self, api: Any, repo: str) -> None:
        self.api = api
        self.repo = repo

    def toc(self) -> list[TocItem]:
        return list(self.api.toc(self.repo))

    def docs(self) -> list[Doc]:
        return list(self.api.docs(self.repo))

    def read(self, doc_id: int) -> Doc:
        return self.api.doc(self.repo, str(doc_id))


# ---------------------------------------------------------------- 选项 / 报告
@dataclass
class Options:
    """一轮处理的可调参数（由 CLI 组装）。"""

    repo: str
    outdir: Path
    host: str = ""
    dry_run: bool = False
    notify_kinds: tuple[str, ...] = NOTICE_KINDS
    default_people: int = rules.DEFAULT_PEOPLE
    default_borrow_type: str = rules.DEFAULT_BORROW_TYPE
    draft_scan_lines: int = rules.DRAFT_SCAN_LINES
    scope_title: str = ""  # 只处理该目录（TITLE 节点）子树内的文档；空 = 全库
    guide: str = ""  # 指导文档 doc_id 或 slug（首次可用来自动识别）
    delete_grace_rounds: int = 2  # 连续 N 轮看不到、且单独读也 404，才算「被删除」
    # 文档消失后**默认保留一条墓碑记录**：万一它又回来了（语雀回收站恢复、或 webhook 误报删除），
    # 墓碑能让 agent 认出「这篇已经处理过」，不会重复受理、重复通知、覆盖申请文件。
    purge_deleted: bool = False  # 想彻底清干净就打开它
    force_all: bool = False  # 无视 updated_at，把所有文档的正文都重读一遗
    tidy_toc: bool = False  # 每轮顺手整理目录（归档过期周目录、归档区置底、只留一个活跃目录）


@dataclass
class RoundReport:
    """一轮处理的结果（CLI 的 ``--json`` 直接输出它）。"""

    repo: str = ""
    at: str = ""
    dry_run: bool = False
    snapshot_ok: bool = False
    seen_docs: int = 0
    skipped: Counter = field(default_factory=Counter)
    accepted: list[str] = field(default_factory=list)  # application_id
    rejected: list[str] = field(default_factory=list)  # 文档标题
    unrecognized: list[str] = field(default_factory=list)
    tampered: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    dropped_drafts: list[str] = field(default_factory=list)
    notified: list[Notice] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    tidy: list[str] = field(default_factory=list)  # 目录整理做了什么（只有开了 --tidy-toc 才有）
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "at": self.at,
            "dry_run": self.dry_run,
            "snapshot_ok": self.snapshot_ok,
            "seen_docs": self.seen_docs,
            "skipped": dict(self.skipped),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "unrecognized": self.unrecognized,
            "tampered": self.tampered,
            "deleted": self.deleted,
            "dropped_drafts": self.dropped_drafts,
            "notified": [n.model_dump(mode="json") for n in self.notified],
            "written": self.written,
            "tidy": self.tidy,
            "notes": self.notes,
            "errors": self.errors,
        }

    def summary(self) -> str:
        parts = [
            f"受理 {len(self.accepted)}",
            f"退回 {len(self.rejected)}",
            f"无法识别 {len(self.unrecognized)}",
            f"改动告警 {len(self.tampered)}",
            f"删除 {len(self.deleted)}",
            f"通知 {len(self.notified)}",
        ]
        return "，".join(parts)


# ---------------------------------------------------------------- 工具
def content_hash(title: str, body: str) -> str:
    """标题 + 正文的指纹（标题即活动名称，改标题也算改动）。"""
    raw = f"{title or ''}\n{body or ''}".encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def node_paths(items: Iterable[TocItem]) -> dict[str, list[str]]:
    """``uuid -> [根, …, 父]`` 的祖先标题链。"""
    by_uuid = {i.uuid: i for i in items}
    out: dict[str, list[str]] = {}
    for item in items:
        chain: list[str] = []
        parent = item.parent_uuid
        while parent and parent in by_uuid:
            chain.append(by_uuid[parent].title)
            parent = by_uuid[parent].parent_uuid
        out[item.uuid] = list(reversed(chain))
    return out


def doc_url(host: str, repo: str, slug: str) -> str:
    if not host or not slug:
        return ""
    return f"{host.rstrip('/')}/{repo.strip('/')}/{slug}"


def diff_fields(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """比对两次解析出来的字段，返回人话形式的差异列表。"""
    out: list[str] = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key, ""), new.get(key, "")
        if before != after:
            out.append(f"{key}：「{before or '（空）'}」→「{after or '（空）'}」")
    return out


# ---------------------------------------------------------------- 主流程
class Pipeline:
    """把一轮处理跑完。**不是线程安全**的：同一时刻只跑一轮。"""

    def __init__(
        self,
        source: Source,
        store: Store,
        notifier: Notifier,
        options: Options,
        *,
        now_factory: Callable[[], datetime] | None = None,
        tidy_worker: Callable[[list[TocItem]], list[str]] | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.notifier = notifier
        self.opts = options
        # 目录整理是唯一的写操作，由 CLI 注入（pipeline 自身只依赖只读的 Source 协议）
        self.tidy_worker = tidy_worker
        self._now = now_factory or (lambda: datetime.now(CN))
        self._new_applications: list[Application] = []

    def _migrate_outbox(self, report: RoundReport) -> None:
        """把老的扁平 outbox 搬进按周目录（只做一次，幂等）。"""
        migrate = getattr(self.notifier, "migrate_flat_layout", None)
        if not callable(migrate):
            return
        try:
            moved = migrate()
        except OSError as exc:
            report.errors.append(f"通知目录迁移失败：{exc}")
            return
        if moved:
            report.notes.append(f"把 {len(moved)} 个旧通知文件搬进了按周目录")

    # -- 对外 -------------------------------------------------------------
    def run_once(self, *, force: set[int] | None = None) -> RoundReport:
        """跑一轮。``force`` 里的 doc_id 会被强制重新读正文（webhook 触发时用）。"""
        now = self._now()
        report = RoundReport(repo=self.opts.repo, at=iso(now), dry_run=self.opts.dry_run)
        if not self.opts.dry_run:
            self._migrate_outbox(report)
        try:
            items = self.source.toc()
            docs = self.source.docs()
        except YuqueError as exc:
            report.errors.append(f"读取知识库失败（本轮不判定删除，避免误报）：{exc}")
            return report

        if items and not docs:
            # 目录非空而文档列表为空：几乎一定是接口异常/分页被截断，
            # 绝不能拿这个快照去判定「文档被删了」。
            report.errors.append("文档列表为空但目录非空，本轮按「快照不可信」处理，不判定删除")
            return report

        report.snapshot_ok = True
        report.seen_docs = len(docs)
        path_by_uuid = node_paths(items)
        node_by_doc = {i.doc_id: i for i in items if i.doc_id}
        doc_node_uuids = {i.uuid for i in items if i.doc_id}
        doc_by_id = {d.id: d for d in docs}
        guide_id = self._resolve_guide(items, docs, report)

        for doc in sorted(docs, key=lambda d: d.id):
            node = node_by_doc.get(doc.id)
            path = path_by_uuid.get(node.uuid, []) if node else []
            reason = self._skip_reason(doc, node, path, doc_node_uuids, guide_id)
            if reason:
                report.skipped[reason] += 1
                continue
            try:
                self._process_doc(doc, path, now, report, force or set())
            except YuqueError as exc:
                report.errors.append(f"{doc.title}(#{doc.id}) 读取失败：{exc}")

        if report.snapshot_ok:
            self._detect_deletions(doc_by_id, now, report)

        if not self.opts.dry_run:
            # 目录整理放在最后：先按本轮快照处理文档，再挪目录节点
            if self.opts.tidy_toc and self.tidy_worker is not None:
                try:
                    report.tidy = self.tidy_worker(items)
                except YuqueError as exc:
                    report.errors.append(f"目录整理失败（不影响文档处理）：{exc}")
            self.store.meta.rounds += 1
            self.store.meta.last_round_at = iso(now)  # 先写时间戳，index.json 才是本轮的
            self._rebuild_index()
            self.store.save()
        self._new_applications.clear()
        return report

    def handle_delete(self, doc_id: int, *, source: str = "webhook") -> Notice | None:
        """删除事件：立即处理（不等轮询的防抖窗口），但**仍要自己读一次确认**。

        webhook 的动作映射可能不准（语雀没有公开报文规范），把「移出目录 / 取消发布 /
        回收站恢复」当成删除就会给社员发错消息、还会丢掉本地记录。所以：
        只有单独读确实 404，才认；读得到就当作没删，交给轮询。
        """
        state = self.store.get(doc_id)
        if state is None or state.deleted_at:
            return None
        if not self.confirm_deleted(doc_id):
            return None
        now = self._now()
        notice = self._emit_deleted(state, now, source=source)
        if not self.opts.dry_run:
            self.store.save()
        return notice

    # -- 单篇文档 ---------------------------------------------------------
    def _process_doc(
        self,
        doc: Doc,
        path: list[str],
        now: datetime,
        report: RoundReport,
        force: set[int],
    ) -> None:
        store = self.store
        state = store.get(doc.id)
        if state is not None and state.deleted_at:
            # 文档又出现了（回收站恢复 / 之前的删除是误报）：
            # 清掉墓碑标记，后面按正常流程走——已受理且内容没变的话会被「未变动」跳过，
            # 所以不会重复受理、也不会重复发通知。
            state.deleted_at = ""
            report.notes.append(f"{doc.title}(#{doc.id}) 之前被判定为已删除，现在又出现了")
        changed = self._changed(doc, state, force)

        # 1) 已经提交的：终态，只盯着「有没有人偷偷改」
        if state and state.status == STATUS_SUBMITTED:
            if changed:
                self._check_tampering(doc, state, now, report)
            else:
                report.skipped["已提交·未变动"] += 1
            state.missing_rounds = 0
            return

        # 2) 上次退回且文档没动过 → 不用重复判定
        if state and not changed:
            report.skipped["未变动"] += 1
            state.missing_rounds = 0
            return

        detail = self.source.read(doc.id)
        body = detail.body or ""

        # 3) 草稿标签：唯一的「状态」。有草稿 → 永不处理（连记录都不留）
        marker = rules.draft_marker(body, lines=self.opts.draft_scan_lines)
        if marker:
            if state is not None:
                store.drop(doc.id)
                report.dropped_drafts.append(doc.title)
                report.notes.append(f"{doc.title}(#{doc.id}) 重新加了草稿标记 → 已清除本地记录")
            report.skipped["草稿"] += 1
            return

        author = str((detail.creator or {}).get("name") or "")
        fields = rules.parse_fields(body)
        verdict = self._evaluate(doc, fields, body, author, now)

        if verdict.ok and verdict.activity is not None:
            self._accept(doc, detail, state, fields, verdict, now, report)
        else:
            self._reject(doc, detail, state, fields, verdict, now, report)

    def _evaluate(
        self, doc: Doc, fields: dict[str, str], body: str, author: str, now: datetime
    ) -> rules.Verdict:
        if len(fields) >= rules.TEMPLATE_MIN_FIELDS:
            return rules.evaluate(
                fields,
                title=doc.title,
                author=author,
                now=now,
                default_people=self.opts.default_people,
                default_borrow_type=self.opts.default_borrow_type,
            )
        salvaged = rules.salvage(
            body,
            title=doc.title,
            author=author,
            now=now,
            default_people=self.opts.default_people,
        )
        if salvaged is not None:
            return salvaged
        return rules.unrecognized(
            "既没按模板填写（至少要能看出「日期」「时间」「校区」），也提取不到这些信息"
        )

    # -- 受理 -------------------------------------------------------------
    def _accept(
        self,
        doc: Doc,
        detail: Doc,
        state: DocState | None,
        fields: dict[str, str],
        verdict: rules.Verdict,
        now: datetime,
        report: RoundReport,
    ) -> None:
        activity: Activity = verdict.activity  # type: ignore[assignment]
        digest = content_hash(doc.title, detail.body or "")
        application = Application(
            application_id=make_application_id(activity.date, activity.period, doc.id),
            packaged_at=iso(now),
            source=SourceRef(
                repo=self.opts.repo,
                doc_id=doc.id,
                doc_slug=doc.slug,
                doc_url=doc_url(self.opts.host, self.opts.repo, doc.slug),
                title=doc.title,
                applicant=verdict.fields.get("申请人", ""),
                applicant_raw=fields.get("申请人", ""),
                creator_id=int((detail.creator or {}).get("id") or 0),
                creator_login=str((detail.creator or {}).get("login") or ""),
                creator_name=str((detail.creator or {}).get("name") or ""),
                content_hash=digest,
                doc_updated_at=str(doc.updated_at or ""),
            ),
            activity=activity,
            raw_fields=dict(fields),
            normalizations=list(verdict.fixes),
            warnings=list(verdict.warnings),
        )

        target = self._write_application(application, report)
        self.store.put(
            self._state_from(
                doc,
                detail,
                state,
                now,
                status=STATUS_SUBMITTED,
                digest=digest,
                verdict=verdict,
                fields=fields,
                application_id=application.application_id,
                application_file=str(target) if target else "",
            )
        )
        report.accepted.append(application.application_id)
        self._emit(
            KIND_ACCEPTED,
            doc,
            now,
            report,
            state=self.store.get(doc.id),
            fingerprint=digest,
            warnings=list(verdict.warnings),
            application_id=application.application_id,
            application_file=str(target) if target else "",
            activity=activity,
            extra={"normalizations": list(verdict.fixes)},
        )

    # -- 退回 / 无法识别 ---------------------------------------------------
    def _reject(
        self,
        doc: Doc,
        detail: Doc,
        state: DocState | None,
        fields: dict[str, str],
        verdict: rules.Verdict,
        now: datetime,
        report: RoundReport,
    ) -> None:
        digest = content_hash(doc.title, detail.body or "")
        kind = KIND_UNRECOGNIZED if verdict.tier == "unrecognized" else KIND_REJECTED
        known = bool(state)  # 之前记录过？用于判断要不要重新通知
        self.store.put(
            self._state_from(
                doc,
                detail,
                state,
                now,
                status=(STATUS_UNRECOGNIZED if kind == KIND_UNRECOGNIZED else STATUS_REJECTED),
                digest=digest,
                verdict=verdict,
                fields=fields,
            )
        )
        (report.unrecognized if kind == KIND_UNRECOGNIZED else report.rejected).append(doc.title)
        self._emit(
            kind,
            doc,
            now,
            report,
            state=self.store.get(doc.id),
            fingerprint=verdict.fingerprint,
            reasons=list(verdict.problems),
            warnings=list(verdict.warnings),
            activity=None,
            extra={"first_time": not known, "fixes": list(verdict.fixes)},
        )

    # -- 改动告警 ---------------------------------------------------------
    def _check_tampering(
        self, doc: Doc, state: DocState, now: datetime, report: RoundReport
    ) -> None:
        detail = self.source.read(doc.id)
        body = detail.body or ""
        digest = content_hash(doc.title, body)
        state.doc_updated_at = str(doc.updated_at or "")
        state.missing_rounds = 0
        if digest == state.content_hash:
            state.last_processed_at = iso(now)
            report.skipped["已提交·改后内容等价"] += 1
            return

        fields = rules.parse_fields(body)
        changes = diff_fields(state.raw_fields, fields)
        if doc.title != state.title:
            changes.insert(0, f"标题：「{state.title}」→「{doc.title}」")
        if not fields:  # 正文被清空 / 结构被破坏
            changes.append("（文档已看不出原来的字段结构）")
        state.content_hash = digest
        state.last_processed_at = iso(now)
        state.title = doc.title
        state.raw_fields = dict(fields)
        report.tampered.append(doc.title)
        self._emit(
            KIND_TAMPERED,
            doc,
            now,
            report,
            state=state,
            fingerprint=digest,  # 每次改动只提醒一次
            reasons=changes,
        )

    # -- 删除 -------------------------------------------------------------
    def _detect_deletions(
        self, doc_by_id: dict[int, Doc], now: datetime, report: RoundReport
    ) -> None:
        for state in list(self.store.entries()):
            if state.doc_id in doc_by_id:
                state.missing_rounds = 0
                continue
            if state.deleted_at:
                continue
            state.missing_rounds += 1
            if state.missing_rounds < max(1, self.opts.delete_grace_rounds):
                report.skipped["疑似消失（等确认）"] += 1
                continue
            # 列表里没有 ≠ 一定被删了。单独读一次：只有 404 才敢下结论。
            if not self.confirm_deleted(state.doc_id):
                report.skipped["消失但读取未确认（再等一轮）"] += 1
                continue
            self._emit_deleted(state, now, source="poll", report=report)

    def confirm_deleted(self, doc_id: int) -> bool:
        """单独读一次目标文档，确认它真的没了（列表异常/权限抖动都不算）。"""
        try:
            self.source.read(doc_id)
        except NotFoundError:
            return True
        except KeyError:  # 测试用的假数据源
            return True
        except YuqueError:
            return False  # 读不到 ≠ 不存在，等下一轮
        return False

    def _emit_deleted(
        self,
        state: DocState,
        now: datetime,
        *,
        source: str = "poll",
        report: RoundReport | None = None,
    ) -> Notice | None:
        kind = KIND_DELETED_SUBMITTED if state.status == STATUS_SUBMITTED else KIND_DELETED_REJECTED
        doc = Doc(id=state.doc_id, slug=state.slug, title=state.title)
        state.deleted_at = iso(now)
        state.missing_rounds = 0
        if report is not None:
            report.deleted.append(state.title)
        notice = self._emit(
            kind,
            doc,
            now,
            report,
            state=state,
            fingerprint="deleted",
            extra={"detected_by": source, "last_status": state.status},
        )
        if self.opts.purge_deleted:
            self.store.drop(state.doc_id)
        return notice

    # -- 通知 -------------------------------------------------------------
    def _emit(
        self,
        kind: str,
        doc: Doc,
        now: datetime,
        report: RoundReport | None,
        *,
        state: DocState | None = None,
        fingerprint: str = "",
        reasons: list[str] | None = None,
        warnings: list[str] | None = None,
        application_id: str = "",
        application_file: str = "",
        activity: Activity | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Notice | None:
        if kind not in self.opts.notify_kinds:
            if report is not None:
                report.skipped[f"通知已关闭({kind})"] += 1
            return None
        if state is not None and self.store.already_notified(doc.id, kind, fingerprint):
            if report is not None:
                report.skipped[f"通知已发过({kind})"] += 1
            return None

        notice = notify_mod.make_notice(
            seq=self.store.next_seq(),
            kind=kind,
            now=now,
            repo=self.opts.repo,
            doc=NoticeDoc(
                repo=self.opts.repo,
                doc_id=doc.id,
                slug=doc.slug,
                title=doc.title,
                url=doc_url(self.opts.host, self.opts.repo, doc.slug),
            ),
            member=NoticeMember(
                yuque_id=state.author_id if state else 0,
                yuque_login=state.author_login if state else "",
                name=state.author_name if state else "",
                applicant_raw=(state.raw_fields.get("申请人", "") if state else ""),
            ),
            application_id=application_id,
            application_file=application_file,
            reasons=reasons,
            warnings=warnings,
            date=activity.date if activity else "",
            period=activity.period if activity else "",
            campus=activity.campus if activity else "",
            extra=extra,
        )
        if not self.opts.dry_run:
            self.notifier.send(notice)
            if state is not None:
                self.store.mark_notified(doc.id, kind, fingerprint)
        if report is not None:
            report.notified.append(notice)
        return notice

    # -- 落盘 -------------------------------------------------------------
    def _write_application(self, application: Application, report: RoundReport) -> Path | None:
        relative = Path("applications") / f"{application.application_id}.json"
        self._new_applications.append(application)
        if self.opts.dry_run:
            return relative
        target = Path(self.opts.outdir) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(dumps(application), encoding="utf-8")
        os.replace(tmp, target)
        report.written.append(str(target))
        return target

    def _rebuild_index(self) -> None:
        """``applications/index.json``：给「发起借用」那一侧当索引，每轮重建。

        **扫目录**而不是扫 state.json：源文档被删除、state 记录被清掉之后，
        申请文件仍然存在，索引里也必须还能看到它（否则消费方会漏单）。
        """
        directory = Path(self.opts.outdir) / "applications"
        rows: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            if path.name.startswith("index"):  # index.json / index-<周>.json 都是索引，不是申请
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            activity = data.get("activity") or {}
            source = data.get("source") or {}
            rows.append(
                {
                    "application_id": data.get("application_id", ""),
                    "doc_id": source.get("doc_id", 0),
                    "doc_title": source.get("title", ""),
                    "file": f"applications/{path.name}",
                    "activity": activity,
                    "packaged_at": data.get("packaged_at", ""),
                }
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.store.meta.last_round_at,
            "repo": self.opts.repo,
            "counts": self.store.counts(),
            "applications": rows,
        }
        # 全量索引 + 本周索引（「重新构建本周的申请 json 索引」；全量契约不变，消费方不用改）
        today = self._now().date()
        this_week = week_mod.week_of(today).title

        def _week_of_row(row: dict[str, Any]) -> str:
            stamp = row.get("packaged_at")
            try:
                day = datetime.fromisoformat(str(stamp)).date() if stamp else today
            except ValueError:
                day = today
            return week_mod.week_of(day).title

        weekly = [row for row in rows if _week_of_row(row) == this_week]
        directory.mkdir(parents=True, exist_ok=True)
        for target, data in (
            (directory / "index.json", payload),
            (
                directory / f"index-{this_week}.json",
                {**payload, "week": this_week, "applications": weekly},
            ),
        ):
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, target)

    # -- 小工具 -----------------------------------------------------------
    def _changed(self, doc: Doc, state: DocState | None, force: set[int]) -> bool:
        if state is None:
            return True
        if doc.id in force or self.opts.force_all:
            return True
        if not state.doc_updated_at or not doc.updated_at:
            return True  # 拿不到时间戳 → 保守地读一次正文
        return state.doc_updated_at != doc.updated_at

    @staticmethod
    def _state_from(
        doc: Doc,
        detail: Doc,
        state: DocState | None,
        now: datetime,
        *,
        status: str,
        digest: str,
        verdict: rules.Verdict,
        fields: dict[str, str],
        application_id: str = "",
        application_file: str = "",
    ) -> DocState:
        """按判定结果造一个新的 :class:`DocState`（保留首次发现时间与已有通知记录）。"""
        creator = detail.creator or {}
        new = DocState(
            doc_id=doc.id,
            slug=doc.slug,
            title=doc.title,
            author_id=int(creator.get("id") or 0),
            author_login=str(creator.get("login") or ""),
            author_name=str(creator.get("name") or ""),
            status=status,
            content_hash=digest,
            doc_updated_at=str(doc.updated_at or ""),
            first_seen_at=(state.first_seen_at if state else iso(now)),
            last_processed_at=iso(now),
            application_id=application_id,
            application_file=application_file,
            problems=list(verdict.problems),
            warnings=list(verdict.warnings),
            notified=dict(state.notified) if state else {},
            raw_fields=dict(fields),
            activity=verdict.activity.model_dump(mode="json") if verdict.activity else {},
        )
        return new

    def _skip_reason(
        self,
        doc: Doc,
        node: TocItem | None,
        path: list[str],
        doc_node_uuids: set[str],
        guide_id: int,
    ) -> str:
        if doc.type != "Doc" or doc.is_sheet:
            return f"非普通文档（{doc.type}）"
        if guide_id and doc.id == guide_id:
            return "指导文档（结构性）"
        if ARCHIVE_TITLE in path:
            return "已归档"
        if doc.title == LEGACY_LOG_TITLE and (node is None or node.parent_uuid in doc_node_uuids):
            # 挂在某篇申请文档下（旧版行为）或压根没挂进目录的，都当结构性文档跳过
            return "审批日志（旧版遗留）"
        if self.opts.scope_title and self.opts.scope_title not in path:
            return f"不在「{self.opts.scope_title}」目录内"
        return ""

    def _resolve_guide(self, items: list[TocItem], docs: list[Doc], report: RoundReport) -> int:
        """确定「指导文档」的 doc id：按 id 锁定（标题可被改，也可被冒充，不能看标题）。"""
        if self.opts.guide:
            want = self.opts.guide.strip()
            found = next(
                (d for d in docs if str(d.id) == want or d.slug == want),
                None,
            )
            if found is None:
                report.errors.append(f"--guide 指定的文档找不到：{want}")
                return self.store.meta.guide_doc_id
            self.store.meta.guide_doc_id = found.id
            report.notes.append(f"指导文档锁定为 #{found.id} {found.title}")
            return found.id

        if self.store.meta.guide_doc_id:
            return self.store.meta.guide_doc_id

        root_docs = {i.doc_id for i in items if i.doc_id and not i.parent_uuid}
        mounted = {i.doc_id for i in items if i.doc_id}
        for doc in docs:
            if doc.type != "Doc":
                continue
            if not (doc.id in root_docs or doc.id not in mounted):
                continue
            if any(hint in doc.title for hint in GUIDE_HINTS):
                self.store.meta.guide_doc_id = doc.id
                report.notes.append(f"首次自动识别指导文档：#{doc.id} {doc.title}")
                return doc.id
        report.notes.append(
            "未能识别指导文档（可用 --guide <doc_id|slug> 指定）；"
            "未指定时，指导文档若不带草稿标记会被判为「无法识别」"
        )
        return 0


def build_pipeline(
    api: Any,
    *,
    options: Options,
    store: Store,
    notifier: Notifier,
    tidy_worker: Any = None,
) -> Pipeline:
    """便捷工厂：组装 API 数据源 + 状态 + 通知出口（+ 可选的目录整理回调）。"""
    return Pipeline(ApiSource(api, options.repo), store, notifier, options, tidy_worker=tidy_worker)
