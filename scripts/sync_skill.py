"""把内置 skill（``src/nju_yuque/data/SKILL.md``）同步到仓库内的 harness 目录。

唯一事实来源是 ``src/nju_yuque/data/SKILL.md``（随 wheel 分发）；
``.pi/skills/yuque/SKILL.md`` 只是它的仓库内副本，供本仓库的 AI harness 直接读取。

用法::

    uv run python scripts/sync_skill.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 GBK，会导致中文 / 符号乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "nju_yuque" / "data" / "SKILL.md"
TARGETS = (ROOT / ".pi" / "skills" / "yuque" / "SKILL.md",)


def main() -> int:
    text = SOURCE.read_text(encoding="utf-8")
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            print(f"= 已一致 {target.relative_to(ROOT)}")
            continue
        target.write_text(text, encoding="utf-8")
        print(f"✓ 已同步 {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
