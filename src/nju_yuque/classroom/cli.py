"""``yuque classroom`` 子命令：教室申请 agent 的运维入口。

| 命令 | 用途 |
|---|---|
| ``once`` | 跑一轮（cron / 手动） |
| ``run`` | 轮询常驻（没有公网入口时的兜底形态） |
| ``serve`` | webhook + 轮询常驻（推荐：实时 + 可靠） |
| ``tidy`` | 整理目录：归档过期周目录 → 归档区、归档区置底、根目录只留一个活跃目录（默认 dry-run） |
| ``status`` | 看本地状态（哪些受理了、哪些退回了）——**不联网** |
| ``outbox`` | 看/确认通知队列（qqbot 侧也可以直接操作目录） |
| ``parse`` | 解析一条 webhook 原始报文（对接排查用） |
| ``schema`` | 打印/导出申请 JSON 契约（JSON Schema） |

所有命令默认**真的执行**（agent 对语雀只读，副作用只有本地文件 + 通知），
想先看效果加 ``--dry-run``。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from ..api import YuqueApi
from ..errors import NotLoggedInError, YuqueError
from ..session import Credentials
from . import contract, rules
from . import notify as notify_mod
from . import toc as toc_mod
from .notify import NOTICE_KINDS, NullNotifier, build_notifier
from .pipeline import Options, build_pipeline
from .server import WEBHOOK_PATH, Runner, parse_webhook
from .store import STATUS_SUBMITTED, default_outdir, open_store
from .week import CN as WEEK_CN

console = Console(legacy_windows=False)
err_console = Console(stderr=True, legacy_windows=False)

classroom_app = typer.Typer(
    no_args_is_help=True,
    help="教室借用申请 agent：读语雀 → 判定要素 → 产出申请 JSON + 通知（对语雀只读）",
)
outbox_app = typer.Typer(no_args_is_help=True, help="通知 outbox：查看 / 确认 / 清理")
classroom_app.add_typer(outbox_app, name="outbox")


def new_app() -> typer.Typer:
    """给 ``nju_yuque.cli`` 挂载用。"""
    return classroom_app


# ---------------------------------------------------------------- 基础设施
def _fail(message: str, code: int = 1) -> None:
    err_console.print(f"[red]✗ {message}[/red]")
    raise typer.Exit(code)


def _creds() -> Credentials:
    try:
        cred = Credentials.load()
    except NotLoggedInError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail 已经退出
    if not cred.is_token:
        _fail("classroom agent 需要令牌模式：请运行 `yuque login --token <令牌>`（只读即可）", 2)
    return cred


def _resolve_repo(repo: str) -> str:
    value = repo or os.environ.get("YUQUE_CLASSROOM_REPO", "")
    if not value:
        _fail("缺少 --repo（知识库 id 或 group/slug），也可用环境变量 YUQUE_CLASSROOM_REPO")
    return value


def _resolve_outdir(repo: str, outdir: Path | None) -> Path:
    return Path(outdir).expanduser() if outdir else default_outdir(repo)


def _build_options(
    *,
    repo: str,
    outdir: Path,
    host: str,
    dry_run: bool,
    default_people: int,
    default_borrow_type: str,
    scope: str,
    guide: str,
    delete_grace: int,
    purge_deleted: bool,
    tidy_toc: bool = False,
    force_all: bool = False,
    notify_kinds: tuple[str, ...] = NOTICE_KINDS,
) -> Options:
    return Options(
        repo=repo,
        outdir=outdir,
        host=host,
        dry_run=dry_run,
        notify_kinds=notify_kinds,
        default_people=default_people,
        default_borrow_type=default_borrow_type,
        scope_title=scope,
        guide=guide,
        delete_grace_rounds=delete_grace,
        purge_deleted=purge_deleted,
        tidy_toc=tidy_toc,
        force_all=force_all,
    )


def _tidy_worker(api: Any, repo: str, store: Any) -> Any:
    """造一个「目录整理」回调注入 pipeline（pipeline 本身只依赖只读协议）。"""

    def worker(items: list) -> list[str]:
        uncertain = _unsettled_by_folder(items, store)
        plan = toc_mod.plan_tidy(items, today=datetime.now(WEEK_CN).date(), unsettled=uncertain)
        if plan.empty:
            return []
        console.print(f"  [cyan]📁 整理目录[/cyan] {plan.describe()}")
        for warning in plan.warnings:
            err_console.print(f"  [yellow]⚠️ {warning}[/yellow]")
        return toc_mod.apply_tidy(
            api, repo, plan, log=lambda text: console.print(f"  [dim]{text}[/dim]")
        )

    return worker


def _unsettled_by_folder(items: list, store: Any) -> dict[str, int]:
    """每个目录里还有几份「已受理但未结案」的申请（归档前告警用）。"""
    out: dict[str, int] = {}
    for folder in [i for i in items if i.type == "TITLE"]:
        count = 0
        for node in items:
            if node.parent_uuid != folder.uuid or not node.doc_id:
                continue
            state = store.get(node.doc_id)
            if state is not None and state.status == "submitted" and not state.deleted_at:
                count += 1
        if count:
            out[folder.title] = count
    return out


def _open_store(name: str, outdir: Path | None) -> tuple[Any, Path]:
    """打开本地状态并校验 outdir 没串库（同一个 outdir 只能服务一个知识库）。"""
    root = _resolve_outdir(name, outdir)
    try:
        store, root = open_store(root, name)
    except ValueError as exc:  # state.json 损坏
        _fail(str(exc))
        raise  # pragma: no cover
    if store.repo and store.repo != name:
        _fail(f"{root / 'state.json'} 属于另一个知识库（{store.repo}），请换 --outdir")
    return store, root


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _positive(value: int, flag: str) -> int:
    if value <= 0:
        _fail(f"{flag} 必须大于 0（当前 {value}）", 2)
    return value


def _kinds(spec: list[str] | None) -> tuple[str, ...]:
    """把 ``--notify-kinds`` 的值解析成事件类型集合（``none`` = 全关）。"""
    if not spec:
        return NOTICE_KINDS
    values = [x.strip() for item in spec for x in item.split(",") if x.strip()]
    if not values or values == ["none"]:
        return ()
    unknown = [v for v in values if v not in NOTICE_KINDS]
    if unknown:
        _fail(f"未知的通知类型：{unknown}（可选 {list(NOTICE_KINDS)}，或 none）", 2)
    return tuple(values)


# ---------------------------------------------------------------- 公共参数
RepoOpt = Annotated[
    str, typer.Option("--repo", help="知识库 id 或 group/slug（默认读 $YUQUE_CLASSROOM_REPO）")
]
OutdirOpt = Annotated[
    Path | None,
    typer.Option(
        "--outdir", help="输出目录（state/applications/notify），默认 ~/.yuque/classroom/<repo>"
    ),
]
HostOpt = Annotated[str, typer.Option("--host", help="语雀域名（默认取登录信息）")]
NotifyOpt = Annotated[
    str, typer.Option("--notify", help="通知出口：outbox（默认）/ console / none，可逗号叠加")
]
PeopleOpt = Annotated[int, typer.Option("--default-people", help="人数没填时的默认值")]
BorrowTypeOpt = Annotated[str, typer.Option("--borrow-type", help="借用类型（默认团学活动）")]
ScopeOpt = Annotated[
    str, typer.Option("--scope", help="只处理该目录（TITLE 节点）子树内的文档；默认全库")
]
GuideOpt = Annotated[
    str, typer.Option("--guide", help="指导文档 doc_id 或 slug（首次可自动识别，之后按 id 锁定）")
]
GraceOpt = Annotated[
    int,
    typer.Option(
        "--delete-grace", help="连续几轮看不到该文档才算「被删除」（默认 2 轮，避免误报）"
    ),
]
TidyOpt = Annotated[
    bool,
    typer.Option(
        "--tidy-toc",
        help="每轮顺手整理目录（归档过期周目录→归档区、归档区置底、根目录只留一个活跃目录）",
    ),
]
PurgeDeletedOpt = Annotated[
    bool,
    typer.Option(
        "--purge-deleted",
        help="文档消失后直接删掉本地记录（默认保留墓碑，以防文档又被恢复/webhook 误报删除）",
    ),
]
KindsOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--notify-kinds",
        help="只发这些事件类型（可重复 / 逗号分隔）；none = 全关（只落盘不通知）",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="输出 JSON")]


# ---------------------------------------------------------------- 一轮
@classroom_app.command("once")
def once(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    host: HostOpt = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只打印计划，不落盘、不通知")] = False,
    notify: NotifyOpt = "outbox",
    default_people: PeopleOpt = rules.DEFAULT_PEOPLE,
    default_borrow_type: BorrowTypeOpt = rules.DEFAULT_BORROW_TYPE,
    scope: ScopeOpt = "",
    guide: GuideOpt = "",
    delete_grace: GraceOpt = 2,
    purge_deleted: PurgeDeletedOpt = False,
    kinds: KindsOpt = None,
    tidy_toc: TidyOpt = False,
    force: Annotated[
        bool, typer.Option("--force", help="无视 updated_at，重读所有文档正文")
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """跑一轮：读知识库 → 判定 → 写申请 JSON / 发通知 / 更新本地状态。"""
    name = _resolve_repo(repo)
    store, root = _open_store(name, outdir)
    default_people = _positive(default_people, "--default-people")

    cred = _creds()
    opts = _build_options(
        repo=name,
        outdir=root,
        host=host or cred.host,
        dry_run=dry_run,
        default_people=default_people,
        default_borrow_type=default_borrow_type,
        scope=scope,
        guide=guide,
        delete_grace=delete_grace,
        purge_deleted=purge_deleted,
        tidy_toc=tidy_toc,
        force_all=force,
        notify_kinds=_kinds(kinds),
    )
    notifier = NullNotifier() if dry_run else build_notifier(notify, root)
    with YuqueApi(cred.host, cred.token, group=cred.group) as api:
        pipeline = build_pipeline(
            api,
            options=opts,
            store=store,
            notifier=notifier,
            tidy_worker=_tidy_worker(api, name, store) if tidy_toc else None,
        )
        report = pipeline.run_once()

    if json_out:
        sys.stdout.write(_dumps(report.to_dict()) + "\n")
    else:
        _print_report(report, root)
    if report.errors:
        # 读取失败 / 单文档失败都要让 cron、脚本能感知，不能静默成功
        raise typer.Exit(1)


def _print_report(report: Any, root: Path) -> None:
    console.print(f"[bold]# {report.repo}[/bold]  {report.at}   dry_run={report.dry_run}")
    for err in report.errors:  # 无论快照成功与否都要能看到失败原因
        err_console.print(f"[red]✗ {err}[/red]")
    if not report.snapshot_ok:
        return
    console.print(f"知识库 {report.seen_docs} 篇　{report.summary()}　outdir={root}")
    for note in report.notes:
        console.print(f"  · {note}")
    for app_id in report.accepted:
        console.print(f"  [green]✅ 已受理[/green] {app_id}")
    for title in report.rejected:
        console.print(f"  [yellow]↩️ 退回[/yellow] {title}")
    for title in report.unrecognized:
        console.print(f"  [yellow]❓ 无法识别[/yellow] {title}")
    for title in report.tampered:
        console.print(f"  [magenta]🔒 已提交后被改动[/magenta] {title}")
    for title in report.deleted:
        console.print(f"  [magenta]🗑️ 文档消失[/magenta] {title}")
    if report.skipped:
        console.print(f"  跳过：{dict(report.skipped)}")
    if report.notified:
        console.print(f"  通知 {len(report.notified)} 条 → {root / 'notify' / 'pending'}")
    if report.dry_run:
        console.print("  （dry-run：没有落盘、也没有发通知）")


# ---------------------------------------------------------------- 常驻
@classroom_app.command("run")
def run(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    host: HostOpt = "",
    interval: Annotated[float, typer.Option("--interval", help="轮询间隔（秒，最小 1）")] = 60.0,
    notify: NotifyOpt = "outbox",
    default_people: PeopleOpt = rules.DEFAULT_PEOPLE,
    default_borrow_type: BorrowTypeOpt = rules.DEFAULT_BORROW_TYPE,
    scope: ScopeOpt = "",
    guide: GuideOpt = "",
    delete_grace: GraceOpt = 2,
    purge_deleted: PurgeDeletedOpt = False,
    kinds: KindsOpt = None,
    tidy_toc: TidyOpt = False,
) -> None:
    """轮询常驻（没有公网入口时用这个）。Ctrl-C 退出。"""
    name = _resolve_repo(repo)
    store, root = _open_store(name, outdir)
    cred = _creds()
    opts = _build_options(
        repo=name,
        outdir=root,
        host=host or cred.host,
        dry_run=False,
        default_people=default_people,
        default_borrow_type=default_borrow_type,
        scope=scope,
        guide=guide,
        delete_grace=delete_grace,
        purge_deleted=purge_deleted,
        tidy_toc=tidy_toc,
        notify_kinds=_kinds(kinds),
    )
    with YuqueApi(cred.host, cred.token, group=cred.group) as api:
        pipeline = build_pipeline(
            api,
            options=opts,
            store=store,
            notifier=build_notifier(notify, root),
            tidy_worker=_tidy_worker(api, name, store) if tidy_toc else None,
        )
        runner = Runner(pipeline, store, interval=interval)
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]收到 Ctrl-C，已停止[/dim]")
        finally:
            runner.stop()


@classroom_app.command("serve")
def serve(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    host: HostOpt = "",
    interval: Annotated[float, typer.Option("--interval", help="轮询兜底间隔（秒）")] = 60.0,
    listen: Annotated[str, typer.Option("--listen", help="webhook 监听地址")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", help="webhook 监听端口")] = 8765,
    path: Annotated[
        str, typer.Option("--path", help="webhook 路径（要和语雀里填的一致）")
    ] = WEBHOOK_PATH,
    secret: Annotated[
        str, typer.Option("--secret", help="共享密钥（可选）：?token= 或 X-Yuque-Token 头")
    ] = "",
    dump_webhook: Annotated[
        Path | None,
        typer.Option("--dump-webhook", help="把原始 webhook 报文落盘到该目录（核对字段用）"),
    ] = None,
    notify: NotifyOpt = "outbox",
    default_people: PeopleOpt = rules.DEFAULT_PEOPLE,
    default_borrow_type: BorrowTypeOpt = rules.DEFAULT_BORROW_TYPE,
    scope: ScopeOpt = "",
    guide: GuideOpt = "",
    delete_grace: GraceOpt = 2,
    purge_deleted: PurgeDeletedOpt = False,
    kinds: KindsOpt = None,
    tidy_toc: TidyOpt = False,
) -> None:
    """webhook + 轮询常驻（推荐形态）。Ctrl-C 退出。"""
    name = _resolve_repo(repo)
    store, root = _open_store(name, outdir)
    cred = _creds()
    opts = _build_options(
        repo=name,
        outdir=root,
        host=host or cred.host,
        dry_run=False,
        default_people=default_people,
        default_borrow_type=default_borrow_type,
        scope=scope,
        guide=guide,
        delete_grace=delete_grace,
        purge_deleted=purge_deleted,
        tidy_toc=tidy_toc,
        notify_kinds=_kinds(kinds),
    )
    with YuqueApi(cred.host, cred.token, group=cred.group) as api:
        pipeline = build_pipeline(
            api,
            options=opts,
            store=store,
            notifier=build_notifier(notify, root),
            tidy_worker=_tidy_worker(api, name, store) if tidy_toc else None,
        )
        runner = Runner(
            pipeline,
            store,
            interval=interval,
            host=listen,
            port=port,
            path=path,
            secret=secret,
            dump_dir=Path(dump_webhook).expanduser() if dump_webhook else None,
        )
        console.print(
            f"[bold]webhook[/bold] http://{listen}:{port}{path}"
            f"　[dim]（公网 URL 要在语雀知识库 → 设置 → 消息推送 里配置）[/dim]"
        )
        if not secret:
            err_console.print(
                "[yellow]! 没设置 --secret：任何能访问该端口的人都能伪造成语雀事件，"
                "强烈建议加一个随机串并拼到 webhook URL 后面[/yellow]"
            )
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]收到 Ctrl-C，已停止[/dim]")
        finally:
            runner.stop()


# ---------------------------------------------------------------- 目录整理
@classroom_app.command("tidy")
def tidy(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    host: HostOpt = "",
    apply: Annotated[bool, typer.Option("--apply", help="真的执行（默认只打印计划）")] = False,
    json_out: JsonOpt = False,
) -> None:
    """整理知识库目录：归档过期周目录 → 归档区、归档区置底、根目录只留一个活跃目录。

    只移动**目录节点**（TITLE），不改任何文档正文、不建也不删文档。默认 dry-run。
    """
    name = _resolve_repo(repo)
    store, _root = _open_store(name, outdir)
    cred = _creds()
    try:
        with YuqueApi(cred.host, cred.token, group=cred.group) as api:
            items = api.toc(name)
            plan = toc_mod.plan_tidy(
                items,
                today=datetime.now(WEEK_CN).date(),
                unsettled=_unsettled_by_folder(items, store),
            )
            done = toc_mod.apply_tidy(api, name, plan) if apply else []
    except YuqueError as exc:
        _fail(exc)
    if json_out:
        sys.stdout.write(
            _dumps(
                {
                    "repo": name,
                    "active": plan.active_title,
                    "archived": plan.archived,
                    "warnings": plan.warnings,
                    "notes": plan.notes,
                    "ops": [op.describe() for op in plan.ops],
                    "applied": done,
                    "dry_run": not apply,
                }
            )
            + "\n"
        )
        return
    console.print(f"[bold]{name}[/bold]　本周目录 = {plan.active_title}")
    for note in plan.notes:
        console.print(f"  · {note}")
    for warning in plan.warnings:
        err_console.print(f"  [yellow]⚠️ {warning}[/yellow]")
    if plan.empty:
        console.print("  [green]✓[/green] 目录已经是目标形态，无需整理")
    for op in plan.ops:
        console.print(f"  - {op.describe()}")
    if apply:
        console.print(f"  [green]✓[/green] 已执行 {len(done)} 个操作")
    else:
        console.print("  （dry-run；加 --apply 执行）")


# ---------------------------------------------------------------- 本地状态
@classroom_app.command("status")
def status(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """查看本地状态（不联网）：受理 / 退回 / 无法识别分别有哪些文档。"""
    name = _resolve_repo(repo)
    store, root = _open_store(name, outdir)
    entries = store.entries()
    if json_out:
        sys.stdout.write(
            _dumps(
                {
                    "repo": store.repo,
                    "outdir": str(root),
                    "counts": store.counts(),
                    "meta": store.meta.to_dict(),
                    "docs": [e.to_dict() for e in entries],
                    "outbox_pending": len(
                        notify_mod.FileOutboxNotifier(root / "notify").list_pending()
                    ),
                }
            )
            + "\n"
        )
        return
    console.print(f"[bold]{store.repo}[/bold]　outdir={root}")
    console.print(f"状态统计：{store.counts()}　已处理轮次：{store.meta.rounds}")
    for state in store.entries():
        mark = {
            STATUS_SUBMITTED: "[green]已受理[/green]",
            "rejected": "[yellow]已退回[/yellow]",
            "unrecognized": "[yellow]无法识别[/yellow]",
        }.get(state.status, state.status)
        console.print(f"  {mark} #{state.doc_id} {state.title}")
        if state.application_id:
            console.print(f"      application_id={state.application_id}")
        for problem in state.problems:
            console.print(f"      [dim]· {problem}[/dim]")
    pending = notify_mod.FileOutboxNotifier(root / "notify").list_pending()
    if pending:
        console.print(f"待投递通知 {len(pending)} 条（{root / 'notify' / 'pending'}）")


# ---------------------------------------------------------------- outbox
@outbox_app.command("list")
def outbox_list(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """列出还没投递出去的通知事件。"""
    name = _resolve_repo(repo)
    root = _resolve_outdir(name, outdir)
    box = notify_mod.FileOutboxNotifier(root / "notify")
    rows = []
    for path in box.list_pending():
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rows.append({"file": str(path), "error": "无法解析"})
    if json_out:
        sys.stdout.write(_dumps(rows) + "\n")
        return
    if not rows:
        console.print("（没有待投递的通知）")
        return
    table = Table("seq", "kind", "文档", "摘要", show_lines=False)
    for row in rows:
        table.add_row(
            str(row.get("seq", "")),
            str(row.get("kind", "")),
            str((row.get("doc") or {}).get("title", "")),
            str(row.get("summary", ""))[:60],
        )
    console.print(table)


@outbox_app.command("ack")
def outbox_ack(
    seq: Annotated[int, typer.Option("--seq", help="要确认的序号；用 0 表示全部")] = 0,
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """把已投递的事件挪到 done/（qqbot 投完消息后调用，或自己移动文件）。"""
    name = _resolve_repo(repo)
    root = _resolve_outdir(name, outdir)
    box = notify_mod.FileOutboxNotifier(root / "notify")
    moved = box.ack_all() if seq <= 0 else [p for p in [box.ack(seq)] if p]
    if json_out:
        sys.stdout.write(_dumps({"acknowledged": [p.name for p in moved]}) + "\n")
        return
    console.print(f"[green]✓[/green] 已确认 {len(moved)} 条")


@outbox_app.command("purge")
def outbox_purge(
    repo: RepoOpt = "",
    outdir: OutdirOpt = None,
) -> None:
    """清空 done/（保留 outbox.jsonl 审计流水）。"""
    name = _resolve_repo(repo)
    root = _resolve_outdir(name, outdir)
    box = notify_mod.FileOutboxNotifier(root / "notify")
    console.print(f"[green]✓[/green] 已清理 {box.purge_done()} 个文件")


# ---------------------------------------------------------------- 对接排查
@classroom_app.command("parse")
def parse(
    file: Annotated[Path, typer.Argument(help="webhook 原始报文的 JSON 文件")],
    json_out: JsonOpt = False,
) -> None:
    """解析一条 webhook 报文，看 agent 会怎么理解它（对接排查用）。"""
    try:
        payload = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"读不了 webhook 报文：{exc}")
    event = parse_webhook(payload)
    if json_out:
        sys.stdout.write(
            _dumps(
                {
                    "action": event.action,
                    "doc_id": event.doc_id,
                    "title": event.title,
                    "slug": event.slug,
                    "repo": event.repo,
                    "action_type": event.action_type,
                    "usable": event.usable,
                }
            )
            + "\n"
        )
        return
    console.print(f"动作：{event.action}（原始 action_type={event.action_type or '未知'}）")
    console.print(f"文档：#{event.doc_id} {event.title} slug={event.slug}")
    console.print(f"知识库：{event.repo or '(报文里没有)'}")
    console.print("能否直接用：" + ("是，会立即处理" if event.usable else "否，交给轮询兜底"))


@classroom_app.command("schema")
def schema_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="写到文件（默认打印到 stdout）")] = None,
) -> None:
    """打印/导出申请 JSON 契约（JSON Schema，交给消费方校验用）。"""
    text = json.dumps(contract.json_schema(), ensure_ascii=False, indent=2) + "\n"
    if out:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] 已写入 {out}")
        return
    sys.stdout.write(text)


@classroom_app.command("rules")
def rules_cmd() -> None:
    """打印当前生效的业务规则（节次表 / 提前量 / 可借时段），便于和社员对齐。"""
    console.print(f"提前量：≥ {rules.ADVANCE_HOURS} 小时")
    console.print(
        f"可借时段：{_hm(rules.DAY_START)}-{_hm(rules.DAY_END)}"
        f"　午饭不借：{_hm(rules.LUNCH_START)}-{_hm(rules.LUNCH_END)}"
    )
    console.print(f"草稿标记（含则跳过）：{' / '.join(rules.DRAFT_HINTS)}")
    table = Table("节", "真实时间")
    for period, start, end in rules.period_table():
        table.add_row(str(period), f"{start}-{end}")
    console.print(table)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


__all__ = ["classroom_app", "new_app"]
