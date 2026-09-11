"""解析语雀链接 / 简写。

支持的输入形式::

    https://nova.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q   # 完整链接
    https://www.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q     # 站点域名无所谓
    ghxd00/mrge27/bbf1n662v36gd85q                            # 三段简写
    ghxd00/mrge27                                             # 知识库（group/book）
    79635820                                                  # 知识库数字 id
    bbf1n662v36gd85q                                          # 文档 slug（需另给 --repo）

语雀 URL 结构固定为 ``/{login}/{book_slug}/{doc_slug}``，其中 login 可能是个人账号
（``www.yuque.com`` 下）也可能是团队 login（自定义域名下）。我们统一叫 ``group``。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Target:
    """一次定位的目标。"""

    group: str | None = None
    book: str | None = None
    slug: str | None = None
    raw: str = ""

    @property
    def repo(self) -> str | None:
        """``group/book`` 形式的知识库坐标；缺一不可。"""
        if self.group and self.book:
            return f"{self.group}/{self.book}"
        return None

    @property
    def is_numeric_id(self) -> bool:
        return bool(self.raw) and self.raw.isdigit()


def parse_target(text: str) -> Target:
    """把链接或简写解析成 :class:`Target`。"""
    raw = (text or "").strip()
    if not raw:
        return Target()

    if "://" in raw:
        path = urlsplit(raw).path
    else:
        path = raw

    parts = [p for p in path.split("/") if p and p not in {"markdown", "docs"}]
    # 去掉可能的 /login 前缀与尾部锚点
    if parts and parts[0] == "login":
        parts = parts[1:]
    if len(parts) >= 3:
        return Target(group=parts[0], book=parts[1], slug=parts[2], raw=raw)
    if len(parts) == 2:
        return Target(group=parts[0], book=parts[1], raw=raw)
    if len(parts) == 1:
        only = parts[0]
        if only.isdigit():
            return Target(book=only, raw=raw)
        return Target(slug=only, raw=raw)
    return Target(raw=raw)


def resolve_repo(target: Target, repo_option: str | None, default_group: str | None = None) -> str:
    """得到知识库坐标（``namespace`` 或数字 id），供接口直接用。"""
    if target.repo:
        return target.repo
    if repo_option:
        opt = repo_option.strip().strip("/")
        if opt.isdigit() or "/" in opt:
            return opt
        if default_group:
            return f"{default_group}/{opt}"
        return opt
    if target.book and target.book.isdigit():
        return target.book
    if target.book and default_group:
        return f"{default_group}/{target.book}"
    if target.group and target.book:
        return f"{target.group}/{target.book}"
    raise ValueError("无法确定知识库：请传 --repo <id 或 group/slug>，或给出知识库链接")


def resolve_doc(
    target: Target, repo_option: str | None, default_group: str | None = None
) -> tuple[str, str]:
    """得到 ``(知识库坐标, 文档 slug)``。"""
    repo = resolve_repo(target, repo_option, default_group)
    if not target.slug:
        raise ValueError("无法确定文档：请给出文档链接，或传 --slug / 完整链接")
    return repo, target.slug
