"""内置 Skill 的读取与安装。

``SKILL.md`` 随 wheel 分发，位于 ``nju_yuque/data/SKILL.md``。
本模块只负责读取 / 安装，不涉及网络。
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

SKILL_NAME = "yuque"
SKILL_FILENAME = "SKILL.md"
_DATA_PACKAGE = "nju_yuque.data"


def skill_text() -> str:
    """返回内置 SKILL.md 的完整文本。"""
    return resources.files(_DATA_PACKAGE).joinpath(SKILL_FILENAME).read_text(encoding="utf-8")


def skill_path() -> Path:
    """返回内置 SKILL.md 的磁盘路径。"""
    with resources.as_file(resources.files(_DATA_PACKAGE).joinpath(SKILL_FILENAME)) as path:
        return Path(path)


def install(target_root: Path | str, *, force: bool = False) -> Path:
    """把内置 skill 安装到 ``<target_root>/yuque/SKILL.md``。"""
    dest = Path(target_root).expanduser() / SKILL_NAME / SKILL_FILENAME
    if dest.exists() and not force:
        raise FileExistsError(str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(skill_text(), encoding="utf-8")
    return dest
