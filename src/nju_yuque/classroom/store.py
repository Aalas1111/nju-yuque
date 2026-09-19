"""教室申请 agent 的**本地持久化**（记录「哪些文档处理过了、处理成什么」）。

一句话：**状态只存在这个文件里，绝不写回语雀。**

- 位置：``<outdir>/state.json``（默认 ``~/.yuque/classroom/<group>_<slug>/state.json``）。
- 不记录草稿文档：带草稿标记的文档永远不入库（草稿重新去掉标记后按新文档处理）。
- 单进程假设：同一份 state.json **只允许一个 agent 进程**写（常驻服务 + 临时 CLI 请共用
  ``--outdir`` 且不要并发跑两轮）。

状态机（agent 视角）：

```
（无记录）--首次处理--> submitted   要素齐备，已产出申请 JSON；终态，不再接受修改
                    \\-> rejected    要素不对，已通知社员；文档改了会重新审查
                    \\-> unrecognized 看不出是申请，已通知社员；改了会重新审查

rejected / unrecognized --文档被加回草稿标记--> （删除记录，回到「无记录」）
submitted    --文档被加回草稿标记--> 仍然是 submitted（不允许用草稿标记「撤回」）
（任何状态） --文档被删除--> 通知一次，然后删除记录（--keep-deleted 可保留墓碑）
```
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_VERSION = 1

STATUS_SUBMITTED = "submitted"
STATUS_REJECTED = "rejected"
STATUS_UNRECOGNIZED = "unrecognized"
STATUSES = (STATUS_SUBMITTED, STATUS_REJECTED, STATUS_UNRECOGNIZED)


@dataclass
class DocState:
    """一篇申请文档的处理状态。"""

    doc_id: int
    slug: str = ""
    title: str = ""
    author_id: int = 0
    author_login: str = ""
    author_name: str = ""
    status: str = STATUS_REJECTED
    content_hash: str = ""
    doc_updated_at: str = ""
    first_seen_at: str = ""
    last_processed_at: str = ""
    application_id: str = ""
    application_file: str = ""
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 「同一种通知不重复发」：kind -> 指纹（problems 指纹 / 内容哈希）
    notified: dict[str, str] = field(default_factory=dict)
    missing_rounds: int = 0  # 连续多少轮没在知识库里看到（防抖，判定「被删除」）
    deleted_at: str = ""
    raw_fields: dict[str, str] = field(default_factory=dict)  # 上次解析到的字段原值（用于比对改动）
    activity: dict[str, Any] = field(default_factory=dict)  # 上次产出/判定的要素快照

    @property
    def notified_fingerprints(self) -> dict[str, str]:
        return self.notified

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # 手改 / 旧版 state.json 里可能的空值，统一归一到该字段的默认形状
    _STR_FIELDS = (
        "slug",
        "title",
        "author_login",
        "author_name",
        "content_hash",
        "doc_updated_at",
        "first_seen_at",
        "last_processed_at",
        "application_id",
        "application_file",
        "deleted_at",
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocState:
        """宽容地还原：未知字段忽略，``null`` 归一成空值（手改过 state.json 也不会崩）。"""
        known = set(cls.__dataclass_fields__)
        out: dict[str, Any] = {k: v for k, v in (data or {}).items() if k in known}
        for key in cls._STR_FIELDS:
            if not isinstance(out.get(key), str):
                out[key] = ""
        for key in ("doc_id", "author_id", "missing_rounds"):
            if not isinstance(out.get(key), int):
                out[key] = 0
        for key in ("notified", "raw_fields", "activity"):
            if not isinstance(out.get(key), dict):
                out[key] = {}
        for key in ("problems", "warnings"):
            if not isinstance(out.get(key), list):
                out[key] = []
        if out.get("status") not in STATUSES:
            out["status"] = STATUS_REJECTED
        return cls(**out)


@dataclass
class Meta:
    """没人对应、但需要记住的东西。"""

    guide_doc_id: int = 0  # 指导文档：按 id 锁定（标题可被人改，也可被人冒充）
    seq: int = 0  # 通知序号（单调递增）
    rounds: int = 0
    last_round_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Meta:
        known = set(cls.__dataclass_fields__)
        out: dict[str, Any] = {k: v for k, v in (data or {}).items() if k in known}
        for key in ("guide_doc_id", "seq", "rounds"):
            if not isinstance(out.get(key), int):
                out[key] = 0
        if not isinstance(out.get("last_round_at"), str):
            out["last_round_at"] = ""
        return cls(**out)


class Store:
    """``state.json`` 的读写。默认路径由 :func:`default_outdir` 给出。

    带一把可重入锁：常驻进程里 webhook 线程会读 :meth:`counts`（``/healthz``），
    而主循环线程在写，不加锁会撞出 ``dictionary changed size during iteration``。
    """

    def __init__(self, path: Path | str, repo: str) -> None:
        self.path = Path(path).expanduser()
        self.repo = repo
        self.version = STATE_VERSION
        self.docs: dict[int, DocState] = {}
        self.meta = Meta()
        self._lock = threading.RLock()

    # -- 加载 / 保存 ------------------------------------------------------
    def load(self) -> Store:
        if not self.path.exists():
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"state.json 损坏，无法解析：{self.path}（{exc}）") from exc
        self.version = int(data.get("version") or STATE_VERSION)
        self.repo = str(data.get("repo") or self.repo)
        for raw in (data.get("docs") or {}).values():
            state = DocState.from_dict(raw)
            self.docs[state.doc_id] = state
        raw_meta = data.get("meta")
        self.meta = Meta.from_dict(raw_meta if isinstance(raw_meta, dict) else {})
        return self

    def save(self) -> Path:
        """原子落盘（先写临时文件再 rename，避免半截 JSON）。"""
        with self._lock:
            payload = {
                "version": STATE_VERSION,
                "repo": self.repo,
                "saved_at": self.meta.last_round_at,
                "meta": self.meta.to_dict(),
                "docs": {str(k): v.to_dict() for k, v in sorted(self.docs.items())},
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path

    # -- 文档状态 ---------------------------------------------------------
    def get(self, doc_id: int) -> DocState | None:
        return self.docs.get(doc_id)

    def put(self, state: DocState) -> DocState:
        with self._lock:
            self.docs[state.doc_id] = state
        return state

    def drop(self, doc_id: int) -> bool:
        with self._lock:
            return self.docs.pop(doc_id, None) is not None

    def entries(self) -> list[DocState]:
        with self._lock:
            return sorted(list(self.docs.values()), key=lambda s: s.doc_id)

    def counts(self) -> dict[str, int]:
        """按状态计数；墓碑（``deleted_at`` 非空）单独算一类，不混进在办申请里。"""
        out = {status: 0 for status in STATUSES}
        out["deleted"] = 0
        with self._lock:
            for state in list(self.docs.values()):
                if state.deleted_at:
                    out["deleted"] += 1
                    continue
                status = state.status if state.status is not None else STATUS_REJECTED
                out[status] = out.get(status, 0) + 1
        return out

    # -- 通知去重 ---------------------------------------------------------
    def already_notified(self, doc_id: int, kind: str, fingerprint: str) -> bool:
        state = self.get(doc_id)
        return bool(state) and state.notified.get(kind) == fingerprint

    def mark_notified(self, doc_id: int, kind: str, fingerprint: str) -> None:
        state = self.get(doc_id)
        if state:
            state.notified[kind] = fingerprint

    # -- 序号 -------------------------------------------------------------
    def next_seq(self) -> int:
        with self._lock:
            self.meta.seq += 1
            return self.meta.seq


def default_outdir(repo: str, home: Path | None = None) -> Path:
    """默认输出目录：``<YUQUE_HOME>/classroom/<group>_<slug>/``。"""
    from ..config import home_dir

    root = home or home_dir()
    return root / "classroom" / repo.replace("/", "_")


def open_store(outdir: Path | str, repo: str) -> tuple[Store, Path]:
    """打开（或初始化）某个知识库的 state.json，返回 (store, outdir)。"""
    root = Path(outdir).expanduser()
    store = Store(root / "state.json", repo).load()
    return store, root
