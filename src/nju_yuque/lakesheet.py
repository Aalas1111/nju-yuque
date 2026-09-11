"""语雀表格（lakesheet）解码。

语雀「表格」文档的 ``body`` 是一段 JSON::

    {"format": "lakesheet", "version": "3.5.5", "larkJson": true,
     "sheet": "<把 zlib 压缩流按 latin-1 解码后得到的字符串>", ...}

其中 ``sheet`` 的前四字节是 ``78 9c``（zlib 魔数）。所以解码链是::

    json.loads(body) -> j["sheet"].encode("latin-1")
                     -> zlib.decompress(...)
                     -> json.loads(...)          # [{name, data, ...}]

解出来每个 sheet 的 ``data[行][列] = {"v": 值, "s": 样式, "t": 类型}``。
本模块把它整理成规整的二维字符串表 + 表头化 records，供 agent 直接消费。
"""

from __future__ import annotations

import csv
import io
import json
import zlib
from typing import Any

from .errors import YuqueError
from .models import Sheet


def look_like_lakesheet(body: str | dict[str, Any]) -> bool:
    """判断这段 body 是否语雀表格。"""
    try:
        obj = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and "sheet" in obj


def decode(body: str | dict[str, Any]) -> list[Sheet]:
    """把 lakesheet 的 body 解成 :class:`Sheet` 列表。"""
    try:
        obj = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError) as exc:
        raise YuqueError(f"不是合法的 lakesheet JSON：{exc}") from exc
    if not isinstance(obj, dict) or "sheet" not in obj:
        raise YuqueError("不是语雀表格：body 里没有 sheet 字段")

    compressed = obj["sheet"]
    if isinstance(compressed, bytes):
        raw = compressed
    else:
        try:
            raw = str(compressed).encode("latin-1")
        except UnicodeEncodeError as exc:
            raise YuqueError("sheet 字段不是 latin-1 编码的压缩流") from exc

    try:
        plain = zlib.decompress(raw)
    except zlib.error as exc:
        raise YuqueError(f"解压表格失败（zlib）：{exc}") from exc

    try:
        sheets = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise YuqueError(f"表格解压后不是合法 JSON 或不是 UTF-8：{exc}") from exc

    if isinstance(sheets, dict):  # 少数版本可能返回单个 sheet 对象
        sheets = [sheets]
    if not isinstance(sheets, list):
        raise YuqueError(f"表格结构异常：期望 list，得到 {type(sheets).__name__}")

    return [
        Sheet(name=str(s.get("name") or f"Sheet{i + 1}"), rows=_grid(s))
        for i, s in enumerate(sheets)
    ]


def _grid(sheet: dict[str, Any]) -> list[list[str]]:
    """把 ``data[行][列]`` 摊平成二维表，跳过整行空白。"""
    data = sheet.get("data") or {}
    if not isinstance(data, dict) or not data:
        return []

    def _idx(key: str) -> int:
        try:
            return int(key)
        except (TypeError, ValueError):
            return -1

    max_row = max((_idx(r) for r in data), default=-1)
    max_col = -1
    for cells in data.values():
        if isinstance(cells, dict):
            max_col = max(max_col, max((_idx(c) for c in cells), default=-1))
    if max_row < 0 or max_col < 0:
        return []

    rows: list[list[str]] = []
    for r in range(max_row + 1):
        cells = data.get(str(r)) or {}
        row = [_cell_text(cells.get(str(c))) for c in range(max_col + 1)]
        if any(v.strip() for v in row):
            rows.append(row)
    return rows


def _cell_text(cell: Any) -> str:
    """单元格取值：``{"v": ...}`` -> str；已是标量则原样。"""
    if cell is None:
        return ""
    if isinstance(cell, dict):
        value = cell.get("v")
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    if isinstance(cell, (list, tuple)):
        return json.dumps(list(cell), ensure_ascii=False)
    return str(cell)


def to_csv(sheets: list[Sheet]) -> str:
    """导出 CSV（多 sheet 依次拼接）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for sheet in sheets:
        writer.writerows(sheet.rows)
    return buf.getvalue()


def to_records(sheets: list[Sheet]) -> list[dict[str, Any]]:
    """多 sheet 合并成 records（每个 sheet 单独表头化）。"""
    out: list[dict[str, Any]] = []
    for sheet in sheets:
        for record in sheet.to_records():
            record = dict(record)
            record.setdefault("_sheet", sheet.name)
            out.append(record)
    return out
