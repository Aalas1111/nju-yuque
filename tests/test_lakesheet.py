"""表格（lakesheet）解码测试：全部离线，用自造样本。"""

from __future__ import annotations

import json
import zlib

import pytest

from nju_yuque import lakesheet
from nju_yuque.errors import YuqueError


def make_lakesheet(sheets: list[dict]) -> str:
    """按语雀真实格式造一个 lakesheet body。"""
    compressed = zlib.compress(json.dumps(sheets, ensure_ascii=False).encode("utf-8"))
    return json.dumps(
        {
            "format": "lakesheet",
            "version": "3.5.5",
            "larkJson": True,
            "sheet": compressed.decode("latin-1"),
        },
        ensure_ascii=False,
    )


SHEETS = [
    {
        "name": "Sheet1",
        "rowCount": 5,
        "data": {
            "0": {"0": {"v": "申请人"}, "1": {"v": "日期"}, "2": {"v": "人数"}},
            "1": {"0": {"v": "张三"}, "1": {"v": "2026-09-11"}, "2": {"v": 30}},
            "2": {"0": {"v": ""}, "1": {"v": ""}, "2": {"v": ""}},  # 整行空白应被跳过
            "3": {"0": {"v": "李四"}, "1": {"v": "2026-09-12\n晚"}, "2": {"v": 20}},
        },
    }
]


def test_look_like_lakesheet() -> None:
    assert lakesheet.look_like_lakesheet(make_lakesheet(SHEETS))
    assert not lakesheet.look_like_lakesheet("# 普通 Markdown")
    assert not lakesheet.look_like_lakesheet("这不是 JSON")


def test_decode_basic() -> None:
    sheets = lakesheet.decode(make_lakesheet(SHEETS))
    assert len(sheets) == 1
    assert sheets[0].name == "Sheet1"
    assert sheets[0].rows == [
        ["申请人", "日期", "人数"],
        ["张三", "2026-09-11", "30"],
        ["李四", "2026-09-12\n晚", "20"],
    ]


def test_decode_accepts_dict_body() -> None:
    body = json.loads(make_lakesheet(SHEETS))
    assert lakesheet.decode(body)[0].rows[1][0] == "张三"


def test_to_records() -> None:
    records = lakesheet.to_records(lakesheet.decode(make_lakesheet(SHEETS)))
    assert records == [
        {"申请人": "张三", "日期": "2026-09-11", "人数": "30", "_sheet": "Sheet1"},
        {"申请人": "李四", "日期": "2026-09-12\n晚", "人数": "20", "_sheet": "Sheet1"},
    ]


def test_to_csv() -> None:
    csv_text = lakesheet.to_csv(lakesheet.decode(make_lakesheet(SHEETS)))
    lines = csv_text.strip().splitlines()
    assert lines[0] == "申请人,日期,人数"
    assert lines[1] == "张三,2026-09-11,30"
    assert "2026-09-12" in lines[2]  # 含换行的单元格会被 CSV 引号包裹


def test_empty_sheet() -> None:
    sheets = lakesheet.decode(make_lakesheet([{"name": "空表", "data": {}}]))
    assert sheets[0].rows == []


def test_plain_body_is_rejected() -> None:
    with pytest.raises(YuqueError):
        lakesheet.decode("# 只是普通文档")
    with pytest.raises(YuqueError):
        lakesheet.decode('{"format": "markdown", "body": "x"}')


def test_bad_compressed_stream() -> None:
    bad = json.dumps({"format": "lakesheet", "sheet": "not-compressed"})
    with pytest.raises(YuqueError):
        lakesheet.decode(bad)
