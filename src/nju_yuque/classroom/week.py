"""周的计算：目录名（`MMDD-MMDD`）、活跃周、跨周判定。

知识库里的日期目录用「周一到周日」命名（如 `0921-0927`）。所有跟「现在属于哪一周」有关的
判断都收敛在这里，方便单测时把「今天」注入进去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta, timezone

CN = timezone(timedelta(hours=8))

WEEK_TITLE_RE = re.compile(r"^(\d{2})(\d{2})\s*[-~～]\s*(\d{2})(\d{2})")


@dataclass(frozen=True)
class Week:
    """一周（周一到周日）。"""

    start: date
    end: date

    @property
    def title(self) -> str:
        return f"{self.start:%m%d}-{self.end:%m%d}"

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return self.title


def week_of(day: date) -> Week:
    """``day`` 所在的那一周（周一起算）。"""
    monday = day - timedelta(days=day.weekday())
    return Week(monday, monday + timedelta(days=6))


def next_week(day: date) -> Week:
    return week_of(day + timedelta(days=7))


def parse_week_title(title: str, *, today: date) -> Week | None:
    """把 `0921-0927` 解析成日期区间；解析不出来返回 None。

    目录名里没有年份：跨年（`1229-0104`）时按「离 ``today`` 最近的那个年份」解释。
    """
    m = WEEK_TITLE_RE.match((title or "").strip())
    if not m:
        return None
    month1, day1, month2, day2 = (int(x) for x in m.groups())
    best: Week | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            start = date(year, month1, day1)
            end = date(year, month2, day2)
        except ValueError:
            continue
        if end < start:  # 跨年
            end = date(year + 1, month2, day2)
        candidate = Week(start, end)
        if best is None:
            best = candidate
            continue
        # 取「离今天最近」的那个年份解释
        if abs((candidate.start - today).days) < abs((best.start - today).days):
            best = candidate
    return best


def is_week_title(title: str) -> bool:
    return bool(WEEK_TITLE_RE.match((title or "").strip()))
