"""``yuque`` 命令行入口。

设计原则（对齐 crb）：

- 默认输出人类可读文本，``--json`` 输出结构化 JSON（agent 优先读这个）。
- 只读命令在两种登录模式下都能用；评论/写操作只在 Cookie 模式可用，且报错明确。
- 任何凭证内容（令牌、Cookie）都不打印、不打日志。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, auth, lakesheet
from . import config as config_mod
from .api import YuqueApi
from .errors import NotLoggedInError, WrongModeError, YuqueError
from .models import Doc, Member, Repo, as_dict
from .session import Credentials
from .urls import Target, parse_target, resolve_doc
from .web import YuqueWeb

# Windows 控制台默认 GBK，会导致中文乱码；强制 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="语雀（Yuque）自动化 CLI — 读知识库 / 读表格 / 发评论（默认只读）",
)
comment_app = typer.Typer(no_args_is_help=True, help="文档评论：查看 / 新增（需要 Cookie 模式）")
skill_app = typer.Typer(no_args_is_help=True, help="内置 AI Skill：查看 / 安装")
app.add_typer(comment_app, name="comment")
app.add_typer(skill_app, name="skill")

# rich 在 Windows 的 legacy console 下会直接调 Win32 API，管道被关闭时（如 `| head`）
# 会抛 OSError(22)；关掉 legacy 渲染后走普通流写入，BrokenPipe 可被 rich 正常处理。
console = Console(legacy_windows=False)
err_console = Console(stderr=True, legacy_windows=False)


# ---------------------------------------------------------------- 基础设施
def _dump(obj: Any) -> None:
    console.print_json(json.dumps(as_dict(obj), ensure_ascii=False, default=str))


def _fail(exc: Exception, code: int = 1) -> None:
    err_console.print(f"[red]✗ {exc}[/red]")
    raise typer.Exit(code)


def _creds() -> Credentials:
    try:
        return Credentials.load()
    except NotLoggedInError as exc:
        _fail(exc, 2)


def _api(cred: Credentials) -> YuqueApi:
    if not cred.is_token:
        _fail(WrongModeError("该命令需要令牌模式：请运行 `yuque login --token <令牌>`"), 2)
    return YuqueApi(cred.host, cred.token, group=cred.group)


def _web(cred: Credentials) -> YuqueWeb:
    if not cred.is_cookie:
        _fail(
            WrongModeError(
                "该命令需要网页登录态：请运行 `yuque login`（弹浏览器登录）或 `yuque login --cookie '<Cookie>'`"
            ),
            2,
        )
    return YuqueWeb(cred)


def _read_docs(cred: Credentials, repo: str, *, limit: int | None = None) -> list[Doc]:
    """列文档，两种模式都支持。"""
    if cred.is_token:
        with _api(cred) as api:
            return api.docs(repo, limit=limit)
    book_id = _book_id_from_repo(cred, repo)
    with _web(cred) as web:
        raw = web.docs(book_id, limit=limit)
    return [_doc_from_web(x) for x in raw]


def _book_id_from_repo(cred: Credentials, repo: str) -> int:
    if repo.isdigit():
        return int(repo)
    slug = repo.split("/")[-1]
    with _web(cred) as web:
        for book in web.books():
            if str(book.get("slug")) == slug:
                return int(book["id"])
    raise YuqueError(
        f"在 Cookie 模式下找不到知识库 `{repo}`；请确认它是你的团队知识库，或改用令牌模式"
    )


def _doc_from_web(raw: dict[str, Any]) -> Doc:
    return Doc(
        id=int(raw.get("id") or 0),
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        type=str(raw.get("type") or "Doc"),
        book_id=raw.get("book_id"),
        format=raw.get("format"),
        public=raw.get("public"),
        word_count=raw.get("word_count"),
        comments_count=raw.get("comments_count"),
        read_count=raw.get("read_count"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        content_updated_at=raw.get("content_updated_at"),
    )


def _fetch_doc(cred: Credentials, target: Target, repo_option: str | None) -> Doc:
    """取一份文档（含正文），两种模式都支持。"""
    if cred.is_token:
        try:
            repo, slug = resolve_doc(target, repo_option, cred.group)
        except ValueError as exc:
            _fail(exc, 2)
        with _api(cred) as api:
            return api.doc(repo, slug)

    # Cookie 模式：内部接口的 book_id 必需，因此要求给完整链接（能拿到 group/book/slug）
    if not (target.group and target.book and target.slug):
        _fail(
            WrongModeError(
                "Cookie 模式读取文档需要完整链接（含 group/book/slug）；"
                "请传 `https://<host>/<group>/<book>/<slug>` 而不是裸 slug"
            ),
            2,
        )
    with _web(cred) as web:
        books = web.books()
        book = next((b for b in books if str(b.get("slug")) == target.book), None)
        detail: dict[str, Any] = {}
        if book:
            detail = web.doc_detail(int(book["id"]), target.slug)
        body = str(detail.get("sourcecode") or detail.get("body") or "")
        if not body or detail.get("format") == "lakesheet":
            try:
                fallback = web.doc_markdown(target.group, target.book, target.slug)
                body = body or fallback
            except YuqueError:
                pass
        if not body:
            _fail(YuqueError(f"取文档正文失败：{target.raw}（内部接口可能已改版）"))
        return Doc(
            id=int(detail.get("id") or 0),
            slug=target.slug,
            title=str(detail.get("title") or target.slug),
            type=str(detail.get("type") or "Doc"),
            book_id=int(detail.get("book_id") or 0) or None,
            format=detail.get("format"),
            updated_at=detail.get("updated_at"),
            body=body,
        )


def _capabilities(cred: Credentials) -> dict[str, bool]:
    """根据登录模式推断能力边界（供 skill / doctor 使用）。"""
    if cred.is_token:
        scopes = {s.strip() for s in (cred.scopes or "").split(",") if s.strip()}
        return {
            "read": True,
            "write": any("write" in s for s in scopes),
            "comment": False,  # 官方接口没有评论能力
        }
    return {
        "read": True,
        "write": bool(cred.csrf_token),
        "comment": bool(cred.csrf_token),
    }


# ---------------------------------------------------------------- 全局选项
def _version_callback(value: bool) -> None:
    if value:
        console.print(f"yuque {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", help="显示版本并退出", is_eager=True, callback=_version_callback
    ),
) -> None:
    """语雀自动化 CLI —— 默认只读；评论/写操作需要 Cookie 模式。"""
    del version


# ---------------------------------------------------------------- 登录
@app.command()
def login(
    token: str | None = typer.Option(None, "--token", help="语雀访问令牌（官方 OpenAPI 模式）"),
    cookie: str | None = typer.Option(
        None, "--cookie", help="直接粘贴浏览器 Cookie 串（网页模式）"
    ),
    browser: str = typer.Option(
        "auto", "--browser", "-b", help="auto / chromium / msedge / chrome"
    ),
    host: str | None = typer.Option(
        None, "--host", help=f"语雀域名（默认 {config_mod.DEFAULT_HOST}）"
    ),
    group: str | None = typer.Option(
        None, "--group", help=f"团队 login（默认 {config_mod.DEFAULT_GROUP}）"
    ),
    timeout: int = typer.Option(300, "--timeout", help="等待浏览器登录完成的秒数"),
    scopes: bool = typer.Option(False, "--show-scopes", help="只显示当前令牌 scope 后退出"),
) -> None:
    """登录：默认开浏览器人工登录；带 --token 则走官方令牌（零浏览器）。"""
    target_host = (host or config_mod.DEFAULT_HOST).rstrip("/")
    target_group = group or config_mod.DEFAULT_GROUP

    if scopes:
        cred = _creds()
        console.print(cred.scopes or "(未知)")
        return

    try:
        if token:
            cred = auth.login_token(token, host=target_host, group=target_group)
            source = "--token"
        elif cookie:
            cred = auth.login_cookie_string(cookie, host=target_host, group=target_group)
            source = "--cookie"
        elif env_token := _env_token():
            cred = auth.login_token(env_token, host=target_host, group=target_group)
            source = "YUQUE_TOKEN 环境变量"
        else:
            console.print("[cyan]即将打开浏览器，请在页面里完成语雀登录……[/cyan]")
            cred = auth.login_browser(
                host=target_host, group=target_group, browser=browser, timeout=timeout
            )
            source = "浏览器登录"
    except YuqueError as exc:
        _fail(exc)

    _print_login_result(cred, source)


def _env_token() -> str | None:
    import os

    value = os.environ.get("YUQUE_TOKEN") or os.environ.get("YUQUE_AUTH_TOKEN")
    return value.strip() if value else None


def _print_login_result(cred: Credentials, source: str) -> None:
    console.print(f"[green]✓[/green] 已登录（{source}）")
    console.print(f"  模式   {cred.mode}")
    console.print(f"  域名   {cred.host}")
    console.print(f"  团队   {cred.group}")
    if cred.name or cred.login:
        console.print(f"  身份   {cred.name or ''} ({cred.login or ''})")
    if cred.scopes:
        console.print(f"  scope  {cred.scopes}")
    caps = _capabilities(cred)
    console.print("  能力   " + " ".join(f"{k}={'✓' if v else '✗'}" for k, v in caps.items()))
    if cred.is_cookie and not cred.csrf_token:
        console.print("[yellow]  提示：Cookie 缺 yuque_ctoken，评论/写操作会被拒绝[/yellow]")


@app.command()
def logout() -> None:
    """删除本地凭证。"""
    from .session import clear

    console.print("✓ 已清除本地凭证" if clear() else "= 本来就没有凭证")


# ---------------------------------------------------------------- 自检
@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="输出 JSON")) -> None:
    """自检：当前登录模式、身份、scope、能力边界。"""
    cred = _creds()
    info: dict[str, Any] = {
        "mode": cred.mode,
        "host": cred.host,
        "group": cred.group,
        "login": cred.login,
        "name": cred.name,
        "scopes": cred.scopes,
        "capabilities": _capabilities(cred),
    }
    if cred.is_token:
        with _api(cred) as api:
            info["hello"] = api.hello()
            info["identity_type"] = api.whoami().get("type")
            info["scopes"] = api.scopes or cred.scopes
    else:
        with _web(cred) as web:
            mine = web.mine()
            info["name"] = mine.get("name") or cred.name
            info["login"] = mine.get("login") or cred.login
            info["has_csrf"] = bool(cred.csrf_token)
    if json_out:
        _dump(info)
        return
    table = Table(show_header=False, box=None)
    for key in ("mode", "host", "group", "name", "login", "scopes"):
        table.add_row(key, str(info.get(key) or ""))
    table.add_row("capabilities", json.dumps(info["capabilities"], ensure_ascii=False))
    console.print(table)


# ---------------------------------------------------------------- 知识库 / 目录
@app.command()
def repos(
    group: str | None = typer.Option(None, "--group", help="团队 login（默认用登录时的）"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出团队成员可见的知识库。"""
    cred = _creds()
    if cred.is_token:
        with _api(cred) as api:
            items = api.repos(group)
    else:
        with _web(cred) as web:
            items = [
                Repo(
                    id=int(b.get("id") or 0),
                    slug=str(b.get("slug") or ""),
                    name=str(b.get("name") or ""),
                    namespace=str(b.get("namespace") or ""),
                    items_count=int(b.get("items_count") or 0),
                    public=int(b.get("public") or 0),
                    description=str(b.get("description") or ""),
                )
                for b in web.books()
            ]
    if json_out:
        _dump(items)
        return
    table = Table("id", "slug", "名称", "文档数", "namespace")
    for r in items:
        table.add_row(str(r.id), r.slug, r.name, str(r.items_count), r.namespace)
    console.print(table)


@app.command()
def toc(
    repo: str = typer.Option(..., "--repo", help="知识库 id 或 group/slug"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """打印知识库目录树。"""
    cred = _creds()
    if not cred.is_token:
        _fail(WrongModeError("目录树目前只在令牌模式可用（官方 /toc 接口）"), 2)
    with _api(cred) as api:
        items = api.toc(repo)
    if json_out:
        _dump(items)
        return
    for item in items:
        _plain(
            f"{'  ' * item.depth}[{item.type}] {item.title}" + (f"  {item.url}" if item.url else "")
        )


def _plain(line: str = "") -> None:
    """给需要按行管道消费的输出用（不经过 rich，避免 Windows 控制台干扰）。"""
    sys.stdout.write(line + "\n")


# ---------------------------------------------------------------- 文档
@app.command()
def docs(
    repo: str = typer.Option(..., "--repo", help="知识库 id 或 group/slug"),
    limit: int = typer.Option(100, "--limit", "-n", help="最多列出多少条"),
    type: str = typer.Option("all", "--type", help="all / Doc / Sheet"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出知识库下的文档（含 updated_at，便于做增量检测）。"""
    cred = _creds()
    try:
        items = _read_docs(cred, repo, limit=limit)
    except YuqueError as exc:
        _fail(exc)
    if type.lower() != "all":
        items = [d for d in items if d.type.lower() == type.lower()]
    if json_out:
        _dump(items)
        return
    table = Table("id", "类型", "更新时间", "标题", "slug")
    for d in items:
        table.add_row(str(d.id), d.type, str(d.updated_at or ""), d.title, d.slug)
    console.print(table)


@app.command()
def search(
    query: str = typer.Argument(..., help="关键词"),
    scope: str | None = typer.Option(None, "--scope", help="限定知识库 group/slug"),
    type: str = typer.Option("doc", "--type", help="doc / repo"),
    page: int = typer.Option(1, "--page", help="页码"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """全文搜索（建议用 --scope 限定知识库）。"""
    cred = _creds()
    if not cred.is_token:
        _fail(WrongModeError("搜索目前只在令牌模式可用（官方 /search 接口）"), 2)
    try:
        with _api(cred) as api:
            result = api.search(query, type=type, scope=scope, page=page)
    except YuqueError as exc:
        _fail(exc)
    if json_out:
        _dump(result)
        return
    meta = result.get("meta") or {}
    console.print(f"total={meta.get('total')} page={meta.get('pageNo')}")
    for item in result.get("data") or []:
        _plain(f"[{item.get('type')}] {item.get('title')}")
        _plain(f"    {item.get('info') or ''}")
        _plain(f"    {item.get('url') or ''}")


@app.command()
def doc(
    target: str = typer.Argument(..., help="文档链接 / group/book/slug / slug"),
    repo: str | None = typer.Option(
        None, "--repo", help="知识库 id 或 group/slug（target 是裸 slug 时必填）"
    ),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
    raw: bool = typer.Option(False, "--raw", help="原样输出正文（不加任何包装）"),
    max_chars: int = typer.Option(20000, "--max-chars", help="正文截断长度"),
) -> None:
    """读取一篇文档的正文（Markdown）。"""
    cred = _creds()
    try:
        item = _fetch_doc(cred, parse_target(target), repo)
    except YuqueError as exc:
        _fail(exc)
    body = item.body or ""
    if item.is_sheet and not raw:
        err_console.print(
            "[yellow]提示：这是语雀表格（Sheet），正文是压缩数据，请用 `yuque table` 子命令[/yellow]"
        )
    if raw:
        sys.stdout.write(body)
        return
    if json_out:
        payload = as_dict(item)
        payload["body"] = body[:max_chars]
        payload["truncated"] = len(body) > max_chars
        _dump(payload)
        return
    console.print(f"# {item.title}")
    console.print(f"# slug={item.slug} format={item.format} updated={item.updated_at}")
    console.print(body[:max_chars])


@app.command()
def table(
    target: str = typer.Argument(..., help="表格文档链接 / group/book/slug"),
    repo: str | None = typer.Option(None, "--repo", help="知识库 id 或 group/slug"),
    sheet: str | None = typer.Option(None, "--sheet", help="只输出指定 sheet"),
    csv_out: bool = typer.Option(False, "--csv", help="输出 CSV"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON（含 records）"),
    limit: int = typer.Option(200, "--limit", "-n", help="最多输出多少行"),
) -> None:
    """读取语雀表格（Sheet）并结构化成二维表 / CSV / records。"""
    cred = _creds()
    try:
        item = _fetch_doc(cred, parse_target(target), repo)
    except YuqueError as exc:
        _fail(exc)
    if not item.body:
        _fail(YuqueError("文档没有正文"))
    if not lakesheet.look_like_lakesheet(item.body):
        _fail(YuqueError(f"不是语雀表格（format={item.format}）；普通文档请用 `yuque doc`"))
    try:
        sheets = lakesheet.decode(item.body)
    except YuqueError as exc:
        _fail(exc)
    if sheet:
        sheets = [s for s in sheets if s.name == sheet]
        if not sheets:
            _fail(YuqueError(f"没有名为 `{sheet}` 的 sheet"))

    if csv_out:
        sys.stdout.write(lakesheet.to_csv(sheets))
        return
    if json_out:
        _dump({"sheets": [as_dict(s) for s in sheets], "records": lakesheet.to_records(sheets)})
        return
    for s in sheets:
        console.print(f"# sheet={s.name} rows={len(s.rows)}")
        for row in s.rows[:limit]:
            console.print(json.dumps(row, ensure_ascii=False))


# ---------------------------------------------------------------- 团队
@app.command()
def members(
    group: str | None = typer.Option(None, "--group", help="团队 login"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出团队成员（user_id → 姓名），用于把意见指名到人。"""
    cred = _creds()
    if not cred.is_token:
        _fail(WrongModeError("成员列表目前只在令牌模式可用"), 2)
    try:
        with _api(cred) as api:
            items: list[Member] = api.members(group)
    except YuqueError as exc:
        _fail(exc)
    if json_out:
        _dump(items)
        return
    for m in items:
        sys.stdout.write(f"{m.user_id}\t{m.name}\t{m.login}\n")


# ---------------------------------------------------------------- 增量监控
@app.command()
def watch(
    repo: str = typer.Option(..., "--repo", help="知识库 id 或 group/slug"),
    since: str | None = typer.Option(None, "--since", help="只列该时间之后更新的文档（ISO8601）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="不更新本地水位线"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出「自上次以来有变化」的文档（增量检测，不依赖搜索索引）。"""
    cred = _creds()
    try:
        items = _read_docs(cred, repo)
    except YuqueError as exc:
        _fail(exc)

    state = config_mod.load_state()
    key = f"{cred.host}|{cred.group}|{repo}"
    last = since or (state.get(key) or {}).get("last_updated_at")
    changed = [d for d in items if d.updated_at and (last is None or str(d.updated_at) > str(last))]
    newest = max((str(d.updated_at) for d in items if d.updated_at), default=last)
    if not dry_run and newest:
        state[key] = {"last_updated_at": newest, "checked_at": _now()}
        config_mod.save_state(state)

    if json_out:
        _dump({"repo": repo, "since": last, "newest": newest, "changed": changed})
        return
    sys.stdout.write(f"# repo={repo} since={last or '(首次)'} 变更 {len(changed)} 篇\n")
    for d in changed:
        sys.stdout.write(f"{d.updated_at}\t{d.type}\t{d.title}\t{d.slug}\n")


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 评论
@comment_app.command("list")
def comment_list(
    target: str = typer.Argument(..., help="文档链接 / slug"),
    repo: str | None = typer.Option(None, "--repo", help="知识库 id 或 group/slug"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出文档评论（需要 Cookie 模式）。"""
    cred = _creds()
    try:
        item = _fetch_doc(cred, parse_target(target), repo)
    except YuqueError as exc:
        _fail(exc)
    if not item.id:
        _fail(YuqueError("拿不到文档 id，无法查评论（请用完整链接）"))
    try:
        with _web(cred) as web:
            items = web.comments(item.id)
    except YuqueError as exc:
        _fail(exc)
    if json_out:
        _dump(items)
        return
    for c in items:
        console.print(f"#{c.get('id')} {c.get('user', {}).get('name', '')}: {c.get('body')}")


@comment_app.command("add")
def comment_add(
    target: str = typer.Argument(..., help="文档链接 / slug"),
    body: str | None = typer.Option(None, "--body", "-m", help="评论正文"),
    file: Path | None = typer.Option(None, "--file", "-f", help="从文件读取评论正文"),
    mention: list[str] | None = typer.Option(
        None, "--mention", help="@ 某人（语雀 login 或姓名），可重复"
    ),
    repo: str | None = typer.Option(None, "--repo", help="知识库 id 或 group/slug"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """在文档下发评论（可 @人，会触发语雀通知）。需要 Cookie 模式。"""
    cred = _creds()
    try:
        item = _fetch_doc(cred, parse_target(target), repo)
    except YuqueError as exc:
        _fail(exc)
    if file:
        body = file.read_text(encoding="utf-8")
    if not body or not body.strip():
        _fail(WrongModeError("评论正文为空：请传 --body 或 --file"), 2)
    if not item.id:
        _fail(YuqueError("拿不到文档 id，无法发评论（请用完整链接）"))
    try:
        with _web(cred) as web:
            group_id = web.group_id(cred.group)
            result = web.create_comment(
                doc_id=item.id,
                body=body,
                mention=list(mention) if mention else None,
                group_id=group_id,
                referer=f"{cred.host}/{cred.group}" if cred.group else None,
            )
    except YuqueError as exc:
        _fail(exc)
    if json_out:
        _dump(result)
    else:
        console.print(f"[green]✓[/green] 评论已发送（id={result.get('id')}）")


# ---------------------------------------------------------------- Skill
@skill_app.command("path")
def skill_path_cmd() -> None:
    """打印内置 SKILL.md 的路径。"""
    from . import skill as skill_mod

    console.print(str(skill_mod.skill_path()))


@skill_app.command("show")
def skill_show_cmd() -> None:
    """原样打印内置 SKILL.md 内容。"""
    from . import skill as skill_mod

    text = skill_mod.skill_text()
    sys.stdout.write(text if text.endswith("\n") else f"{text}\n")


@skill_app.command("install")
def skill_install_cmd(
    directory: Path = typer.Option(
        Path(".pi/skills"), "--dir", "-d", help="AI harness 的 skills 根目录"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="已存在时覆盖"),
) -> None:
    """把内置 skill 安装到 <dir>/yuque/SKILL.md。"""
    from . import skill as skill_mod

    try:
        dest = skill_mod.install(directory, force=force)
    except FileExistsError as exc:
        err_console.print(f"[red]已存在：{exc}（加 --force 覆盖）[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]✓[/green] skill 已安装：{dest}")


def main() -> None:
    """console_scripts 入口：把语雀异常翻译成友好输出。"""
    try:
        app()
    except YuqueError as exc:  # pragma: no cover - 顶层兜底
        _fail(exc)
    except (BrokenPipeError, OSError) as exc:  # `yuque ... | head` 之类的正常收尾
        # Windows 下管道被上游关闭时表现为 OSError(22/EINVAL) 而不是 BrokenPipeError。
        if isinstance(exc, OSError) and exc.errno not in (None, 22, 32):
            raise
        _silence_stdout()


def _silence_stdout() -> None:
    """把 stdout 接到 devnull，避免解释器退出时再打印 “Exception ignored"。"""
    import os

    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass
