"""共享测试夹具：把凭证目录隔离到临时目录，避免碰到真实的 ~/.yuque。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YUQUE_HOME", str(tmp_path))
    return tmp_path
