"""教室借用申请 · **对外数据契约**（详情 JSON 与通知 JSON 的形状）。

这里只放「跨同学交接」的数据结构，**不含任何业务规则**，也不碰网络：

- :class:`Application`：一份要素齐备的借用申请 → 落成
  ``<outdir>/applications/<application_id>.json``，交给负责提交的同学（谷和平）消费。
- :class:`Notice` 在 :mod:`nju_yuque.classroom.notify` 里，是给 qqbot 的事件流。

> 约定：JSON 里所有键名一律**英文**（方便别人写代码），值里的业务名词保留中文
> （``campus="仙林"``、``borrow_type="团学活动"``）。字段含义见
> ``docs/classroom-agent.md``；机器可读版本见 ``docs/classroom-application.schema.json``
> （由 ``Application.model_json_schema()`` 生成，测试保证两者一致）。

契约一旦发布就**只增不改**：加字段必须给默认值，并升 ``schema_version``。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"
APPLICATION_TYPE = "classroom_borrow"


class _Base(BaseModel):
    """统一配置：忽略未知字段（向前兼容），允许按字段名构造。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Activity(_Base):
    """一份申请里「要借什么」的全部要素（CAC 说的「确定要素」）。"""

    name: str = Field(description="活动名称，直接取文档标题")
    date: str = Field(description="借用日期，YYYY-MM-DD")
    start: str = Field(description="活动开始时间，HH:MM")
    end: str = Field(description="活动结束时间，HH:MM")
    period: str = Field(description="借用节次，形如 7-8（单节写 7）")
    period_start: int = Field(description="起始节次（整数）")
    period_end: int = Field(description="结束节次（整数）")
    campus: str = Field(description="校区规范名：鼓楼 / 浦口 / 仙林 / 苏州")
    campus_code: str = Field(description="学校校区代码：1 鼓楼 / 2 浦口 / 3 仙林 / 4 苏州")
    building: str = Field(default="", description="教学楼（空 = 不限 / 随机）")
    room: str = Field(default="", description="意向教室（空 = 随机；不保证借到）")
    people: int = Field(default=30, ge=0, description="教室人数需求")
    people_source: Literal["document", "default"] = Field(
        default="default", description="人数来自文档填写还是默认值"
    )
    borrow_type: str = Field(default="团学活动", description="借用类型（默认团学活动）")


class SourceRef(_Base):
    """申请来源：哪一篇语雀文档、谁写的、当时内容指纹。"""

    repo: str = Field(description="知识库 group/slug")
    doc_id: int = Field(description="语雀文档 id（稳定主键）")
    doc_slug: str = Field(default="", description="语雀文档 slug")
    doc_url: str = Field(default="", description="文档链接")
    title: str = Field(default="", description="文档标题（= 活动名称）")
    applicant: str = Field(default="", description="申请人字段最终取值（可能是推断出来的）")
    applicant_raw: str = Field(default="", description="文档里原样填写的申请人")
    creator_id: int = Field(default=0, description="文档创建者语雀 user id")
    creator_login: str = Field(default="", description="文档创建者语雀 login")
    creator_name: str = Field(default="", description="文档创建者显示名")
    content_hash: str = Field(default="", description="正文指纹 sha256:...，用于判定「文档被改过」")
    doc_updated_at: str = Field(default="", description="语雀侧 updated_at")


class Application(_Base):
    """一份要素齐备的借用申请（交接给「发起借用」模块）。"""

    schema_version: str = Field(default=SCHEMA_VERSION)
    application_type: str = Field(default=APPLICATION_TYPE)
    application_id: str = Field(description="稳定 id：<date>-<period>-<doc_id>")
    packaged_at: str = Field(description="打包时间（UTC+8，ISO8601）")
    source: SourceRef
    activity: Activity
    raw_fields: dict[str, str] = Field(default_factory=dict, description="文档里原样填写的字段")
    normalizations: list[str] = Field(
        default_factory=list, description="agent 做过的自动规范化（人工可复核）"
    )
    warnings: list[str] = Field(
        default_factory=list, description="可疑但放行的提示（如时间过长、12 小时制歧义）"
    )


def make_application_id(date: str, period: str, doc_id: int) -> str:
    """生成稳定、可读、可排序的申请 id。"""
    return f"{date}-{period.replace('-', '_')}-{doc_id}"


def iso(now: datetime) -> str:
    """统一的时间戳写法（秒级精度，保留时区）。"""
    return now.isoformat(timespec="seconds")


def dumps(model: BaseModel) -> str:
    """按契约输出 JSON 文本（UTF-8 友好、缩进 2、行尾换行）。"""
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def json_schema() -> dict[str, Any]:
    """导出 ``Application`` 的 JSON Schema（供别人校验 / 生成代码）。"""
    schema = Application.model_json_schema()
    schema["$id"] = f"https://github.com/Aalas1111/NJU_Yuque/classroom-application/{SCHEMA_VERSION}"
    schema["title"] = "教室借用申请（Application）"
    return schema
