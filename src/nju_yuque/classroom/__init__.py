"""教室借用申请 agent（语雀只读）。

一句话职责：**读社员在语雀里填的申请文档，把「确定要素」抽出来，要素齐备就打包成
固定格式的 JSON，要素不对/文档被改/文档被删就用通知事件告诉社员。**

- 对语雀**只读**：不改正文、不写状态、不建审批日志、不移动目录、不删文档；
  「这份文档处理过没有」只记在本地 ``state.json`` 里。
- 唯一的「状态」是社员自己维护的：**文档开头有草稿标记就不处理**，删掉标记就会被处理。
- 产出三样东西（都在 ``<outdir>``）：
  ``applications/*.json``（给提交方）、``notify/pending/*.json``（给 qqbot）、
  ``state.json``（给自己）。

模块划分：

=====================  ====================================================
:mod:`rules`           纯规则：草稿识别 + 字段归一化 + 节次推算 + 四档判定
:mod:`contract`        对外数据契约（申请 JSON），含 JSON Schema 导出
:mod:`store`           本地持久化（state.json）与状态机
:mod:`notify`          通知事件与文件 outbox
:mod:`pipeline`        一轮处理的编排（唯一写文件的地方）
:mod:`server`          webhook 接收 + 轮询兜底的常驻形态
:mod:`cli`             ``yuque classroom ...`` 子命令
=====================  ====================================================
"""

from __future__ import annotations

from .contract import (
    APPLICATION_TYPE,
    SCHEMA_VERSION,
    Activity,
    Application,
    SourceRef,
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
    Notifier,
    build_notifier,
)
from .pipeline import ApiSource, Options, Pipeline, RoundReport, build_pipeline, content_hash
from .store import DocState, Store, default_outdir, open_store

__all__ = [
    "APPLICATION_TYPE",
    "SCHEMA_VERSION",
    "Activity",
    "ApiSource",
    "Application",
    "DocState",
    "KIND_ACCEPTED",
    "KIND_DELETED_REJECTED",
    "KIND_DELETED_SUBMITTED",
    "KIND_REJECTED",
    "KIND_TAMPERED",
    "KIND_UNRECOGNIZED",
    "NOTICE_KINDS",
    "Notice",
    "Notifier",
    "Options",
    "Pipeline",
    "RoundReport",
    "SourceRef",
    "Store",
    "build_notifier",
    "build_pipeline",
    "content_hash",
    "default_outdir",
    "make_application_id",
    "open_store",
]
