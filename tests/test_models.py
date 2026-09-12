"""数据模型容错测试：语雀对空值/空字符串的处理很不统一。"""

from __future__ import annotations

from nju_yuque.models import Doc, Sheet, TocItem, as_dict


def test_tocitem_tolerates_none_and_blank() -> None:
    # TITLE 节点会返回 doc_id=""/level=""；根节点 parent_uuid=None
    item = TocItem.model_validate(
        {
            "uuid": "u1",
            "type": "TITLE",
            "title": "分组",
            "url": None,
            "slug": None,
            "doc_id": "",
            "level": "",
            "parent_uuid": None,
            "child_uuid": None,
        }
    )
    assert item.doc_id is None
    assert item.level is None
    assert item.parent_uuid == ""
    assert item.url == ""
    assert item.slug == ""


def test_doc_is_sheet() -> None:
    assert Doc.model_validate({"id": 1, "slug": "s", "title": "t", "format": "lakesheet"}).is_sheet
    assert Doc.model_validate({"id": 1, "slug": "s", "title": "t", "type": "Sheet"}).is_sheet
    assert not Doc.model_validate({"id": 1, "slug": "s", "title": "t"}).is_sheet


def test_sheet_records_pad_short_rows() -> None:
    sheet = Sheet(name="S", rows=[["a", "b", "c"], ["1"], ["2", "3"]])
    assert sheet.to_records() == [
        {"a": "1", "b": "", "c": ""},
        {"a": "2", "b": "3", "c": ""},
    ]


def test_sheet_records_without_header() -> None:
    assert Sheet(name="S", rows=[]).to_records() == []


def test_as_dict_recurses() -> None:
    payload = {"items": [Doc.model_validate({"id": 1, "slug": "s", "title": "t"})], "n": 1}
    out = as_dict(payload)
    assert out["items"][0]["title"] == "t"
    assert out["n"] == 1
