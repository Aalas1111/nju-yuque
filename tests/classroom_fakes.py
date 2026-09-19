"""教室申请 pipeline 的测试夹具：完全离线的假语雀（不联网、不发真通知）。

放在 ``tests/`` 下而不进包，因为它只服务于测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nju_yuque.classroom import notify as notify_mod
from nju_yuque.classroom import rules
from nju_yuque.classroom.notify import NOTICE_KINDS, FileOutboxNotifier
from nju_yuque.classroom.pipeline import Options, Pipeline, RoundReport, content_hash
from nju_yuque.classroom.store import Store
from nju_yuque.models import Doc, TocItem

CN = rules.CN
REPO = "lqogh0/jsjysq"
HOST = "https://nova.yuque.com"
NOW = datetime(2026, 9, 12, 9, 0, tzinfo=CN)  # 周六上午 9:00


# ---------------------------------------------------------------- 造数据
def body(**fields: str) -> str:
    """按模板造一份申请正文（字段名固定，值可覆盖）。"""
    base = {
        "申请人": "张三",
        "活动日期": "2026-09-16",
        "活动时间": "16:10-18:00",
        "校区": "仙林",
        "教学楼": "",
        "教室": "",
        "人数": "",
    }
    base.update(fields)
    return "\n".join(f"{k}：{v}" for k, v in base.items() if v is not None) + "\n"


def draft_body(**fields: str) -> str:
    return "【草稿】填完请删掉本行\n\n" + body(**fields)


def make_doc(
    doc_id: int,
    title: str = "新生见面会",
    text: str | None = None,
    *,
    updated_at: str = "2026-09-12T08:00:00+08:00",
    slug: str | None = None,
    type_: str = "Doc",  # noqa: A002
    creator: tuple[int, str, str] | None = (77, "zhangsan", "张三"),
) -> Doc:
    creator_obj = {"id": creator[0], "login": creator[1], "name": creator[2]} if creator else None
    return Doc(
        id=doc_id,
        slug=slug or f"s{doc_id}",
        title=title,
        type=type_,
        body=body() if text is None else text,
        updated_at=updated_at,
        creator=creator_obj,
    )


def toc_doc(uuid: str, doc_id: int, title: str = "", parent: str = "", slug: str = "") -> TocItem:
    return TocItem(
        uuid=uuid,
        type="DOC",
        title=title,
        url="",
        slug=slug or f"s{doc_id}",
        doc_id=doc_id,
        level=None,
        parent_uuid=parent,
        child_uuid="",
    )


def toc_title(uuid: str, title: str, parent: str = "") -> TocItem:
    return TocItem(
        uuid=uuid,
        type="TITLE",
        title=title,
        url="",
        slug="",
        doc_id=None,
        level=None,
        parent_uuid=parent,
        child_uuid="",
    )


# ---------------------------------------------------------------- 假语雀
class FakeSource:
    """只实现 pipeline 需要的三个只读方法。"""

    def __init__(self, docs: list[Doc], items: list[TocItem] | None = None) -> None:
        self.items = list(items or [])
        self.by_id: dict[int, Doc] = {d.id: d for d in docs}
        self.hidden: set[int] = set()  # 列表里看不到、但单独读还能读到（模拟分页被截断）
        self.reads: list[int] = []
        self.fail = False
        self.empty_listing = False  # 列表接口「成功」但返回空

    # -- 变更操作（模拟社员在语雀里改东西）-------------------------------
    def set(self, doc: Doc) -> None:
        self.by_id[doc.id] = doc

    def edit(
        self, doc_id: int, text: str, *, title: str | None = None, ts: str | None = None
    ) -> Doc:
        old = self.by_id[doc_id]
        doc = old.model_copy(
            update={
                "body": text,
                "title": title if title is not None else old.title,
                "updated_at": ts or f"2026-09-12T0{len(self.reads) + 1}:30:00+08:00",
            }
        )
        self.set(doc)
        return doc

    def remove(self, doc_id: int) -> None:
        """真删除：列表里没有、单独读也读不到。"""
        self.by_id.pop(doc_id, None)

    def hide(self, doc_id: int) -> None:
        """只是从列表里消失（单独读仍然成功）——不能据此判定删除。"""
        self.hidden.add(doc_id)

    def mount(self, item: TocItem) -> None:
        self.items.append(item)

    # -- Source 协议 ------------------------------------------------------
    def toc(self) -> list[TocItem]:
        if self.fail:
            from nju_yuque.errors import YuqueError

            raise YuqueError("模拟读取失败")
        return list(self.items)

    def docs(self) -> list[Doc]:  # noqa: A003 - 对齐 Source 协议
        if self.fail:
            from nju_yuque.errors import YuqueError

            raise YuqueError("模拟读取失败")
        if self.empty_listing:
            return []
        return [d for d in self.by_id.values() if d.id not in self.hidden]

    def read(self, doc_id: int) -> Doc:
        if self.fail:
            from nju_yuque.errors import YuqueError

            raise YuqueError("模拟读取失败")
        self.reads.append(doc_id)
        return self.by_id[doc_id]


# ---------------------------------------------------------------- 夹具
@dataclass
class Harness:
    """一次测试里的完整环境：假语雀 + state + outbox。"""

    source: FakeSource
    store: Store
    outdir: Path
    pipeline: Pipeline
    box: FileOutboxNotifier
    runs: list[RoundReport] = field(default_factory=list)

    def run(self, *, force: set[int] | None = None) -> RoundReport:
        report = self.pipeline.run_once(force=force)
        self.runs.append(report)
        return report

    # -- 观察 -------------------------------------------------------------
    @property
    def last(self) -> RoundReport:
        return self.runs[-1]

    def notices(self, kind: str | None = None) -> list[dict]:
        out = []
        for path in self.box.list_pending():
            data = json.loads(path.read_text(encoding="utf-8"))
            if kind is None or data.get("kind") == kind:
                out.append(data)
        return out

    def applications(self) -> list[dict]:
        directory = self.outdir / "applications"
        return [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(directory.glob("*.json"))
            if not p.name.startswith("index")  # index.json / index-<周>.json 都不是申请
        ]

    def index(self) -> dict:
        return json.loads((self.outdir / "applications" / "index.json").read_text("utf-8"))

    def state_file(self) -> dict:
        return json.loads((self.outdir / "state.json").read_text(encoding="utf-8"))


def build_harness(
    tmp_path: Path,
    docs: list[Doc],
    *,
    items: list[TocItem] | None = None,
    now: datetime = NOW,
    store: Store | None = None,
    **option_overrides: object,
) -> Harness:
    """组装一个离线 pipeline。``option_overrides`` 直接覆盖 :class:`Options` 字段。"""
    outdir = tmp_path / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    source = FakeSource(docs, items)
    st = store or Store(outdir / "state.json", REPO)
    options = Options(repo=REPO, outdir=outdir, host=HOST, **option_overrides)  # type: ignore[arg-type]
    box = FileOutboxNotifier(outdir / "notify")
    pipeline = Pipeline(source, st, box, options, now_factory=lambda: now)
    return Harness(source=source, store=st, outdir=outdir, pipeline=pipeline, box=box)


def kinds(*names: str) -> tuple[str, ...]:
    """只开某几种通知。"""
    return tuple(n for n in names if n in NOTICE_KINDS)


__all__ = [
    "CN",
    "HOST",
    "NOW",
    "REPO",
    "FakeSource",
    "Harness",
    "body",
    "build_harness",
    "content_hash",
    "draft_body",
    "kinds",
    "make_doc",
    "notify_mod",
    "rules",
    "toc_doc",
    "toc_title",
]
