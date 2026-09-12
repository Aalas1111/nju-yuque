"""数据模型：把语雀返回的字段收敛成稳定形状，方便 CLI 与 agent 消费。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Repo(_Base):
    """知识库。"""

    id: int
    slug: str
    name: str
    namespace: str = ""
    items_count: int = 0
    public: int = 0
    description: str = ""


class Doc(_Base):
    """文档（含表格 Sheet）。"""

    id: int
    slug: str
    title: str
    type: str = "Doc"
    book_id: int | None = None
    format: str | None = None
    public: int | None = None
    word_count: int | None = None
    comments_count: int | None = None
    read_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    content_updated_at: str | None = None
    body: str | None = None
    description: str | None = None
    # 创作者（列表接口可能缺失，详情接口一定有）；用于「申请人」缺失时回退
    creator: dict[str, Any] | None = None

    @property
    def is_sheet(self) -> bool:
        return (self.format or "") == "lakesheet" or self.type == "Sheet"


class TocItem(_Base):
    """目录项（DOC = 文档，TITLE = 分组标题）。"""

    uuid: str
    type: str
    title: str
    url: str = ""
    slug: str = ""
    doc_id: int | None = None
    level: int | None = None
    parent_uuid: str = ""
    child_uuid: str = ""
    depth: int = 0  # 由 TOC 接口的树结构现算，不是语雀字段

    @field_validator("doc_id", "level", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """语雀对 TITLE 项会返回空字符串，这里统一成 None。"""
        return None if value is None or value == "" else value

    @field_validator("url", "slug", "parent_uuid", "child_uuid", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        """根节点的 parent_uuid 会是 null，统一成空串。"""
        return "" if value is None else value


class Sheet(_Base):
    """解码后的表格页。"""

    name: str = "Sheet1"
    rows: list[list[str]] = Field(default_factory=list)

    def to_records(self) -> list[dict[str, str]]:
        """首行当表头，返回 [{列名: 值}]，方便直接喂给 agent。"""
        if not self.rows:
            return []
        header = [h.strip() or f"col{i}" for i, h in enumerate(self.rows[0])]
        out: list[dict[str, str]] = []
        for row in self.rows[1:]:
            padded = list(row) + [""] * (len(header) - len(row))
            out.append({header[i]: padded[i] for i in range(len(header))})
        return out


class Member(_Base):
    """团队成员。"""

    user_id: int
    name: str = ""
    login: str = ""
    role: int | None = None


def as_dict(obj: Any) -> Any:
    """把 pydantic 模型（含嵌套）递归转成 dict；其它类型原样返回。"""
    if isinstance(obj, BaseModel):
        return as_dict(obj.model_dump())
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_dict(x) for x in obj]
    return obj
