"""路径、环境变量与默认配置。

约定：

- 凭证与状态一律放在 ``~/.yuque/``（可用 ``YUQUE_HOME`` 改写），**永不入库**。
- 默认指向 NOVA 社团空间；换团队用 ``--host / --group`` 或环境变量覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 路径


def home_dir() -> Path:
    override = os.environ.get("YUQUE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".yuque"


def auth_file() -> Path:
    return home_dir() / "auth.json"


def state_file() -> Path:
    return home_dir() / "state.json"


# ---------------------------------------------------------------- 默认值

DEFAULT_HOST = os.environ.get("YUQUE_HOST", "https://nova.yuque.com")
DEFAULT_GROUP = os.environ.get("YUQUE_GROUP", "ghxd00")

BROWSER_CHOICES = ("auto", "chromium", "msedge", "chrome")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- watch 状态


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_file()
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    target = path or state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
