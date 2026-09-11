"""链接 / 简写解析测试。"""

from __future__ import annotations

import pytest

from nju_yuque.urls import Target, parse_target, resolve_doc, resolve_repo


def test_full_url() -> None:
    t = parse_target("https://nova.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q")
    assert (t.group, t.book, t.slug) == ("ghxd00", "mrge27", "bbf1n662v36gd85q")
    assert t.repo == "ghxd00/mrge27"


def test_url_with_www_and_query() -> None:
    t = parse_target("https://www.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q?language=zh-cn#anchor")
    assert t.repo == "ghxd00/mrge27"
    assert t.slug == "bbf1n662v36gd85q"


def test_markdown_suffix_is_stripped() -> None:
    t = parse_target("https://nova.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q/markdown")
    assert t.slug == "bbf1n662v36gd85q"


def test_repo_shorthand() -> None:
    t = parse_target("ghxd00/mrge27")
    assert t.repo == "ghxd00/mrge27"
    assert t.slug is None


def test_three_segment_shorthand() -> None:
    t = parse_target("ghxd00/mrge27/abc123")
    assert (t.group, t.book, t.slug) == ("ghxd00", "mrge27", "abc123")


def test_numeric_id() -> None:
    t = parse_target("79635820")
    assert t.is_numeric_id
    assert resolve_repo(t, None) == "79635820"


def test_bare_slug_needs_repo() -> None:
    with pytest.raises(ValueError):
        resolve_repo(Target(slug="abc123"), None)
    assert resolve_repo(Target(slug="abc123"), "ghxd00/mrge27") == "ghxd00/mrge27"


def test_bare_book_uses_default_group() -> None:
    assert resolve_repo(Target(book="mrge27"), None, "ghxd00") == "ghxd00/mrge27"
    assert resolve_repo(Target(book="mrge27"), "mrge27", "ghxd00") == "ghxd00/mrge27"


def test_resolve_doc() -> None:
    t = parse_target("https://nova.yuque.com/ghxd00/mrge27/abc")
    assert resolve_doc(t, None) == ("ghxd00/mrge27", "abc")
    with pytest.raises(ValueError):
        resolve_doc(parse_target("ghxd00/mrge27"), None)
