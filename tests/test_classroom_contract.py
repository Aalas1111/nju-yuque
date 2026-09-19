"""对外数据契约（申请 JSON）单测，并锁住随仓库分发的 JSON Schema。"""

from __future__ import annotations

import json
from pathlib import Path

from nju_yuque.classroom import contract
from nju_yuque.classroom.contract import Activity, Application, SourceRef, make_application_id

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "docs" / "classroom-application.schema.json"


def test_application_id_is_stable_and_sortable() -> None:
    assert make_application_id("2026-09-16", "7-8", 123) == "2026-09-16-7_8-123"
    assert make_application_id("2026-09-16", "5", 123) == "2026-09-16-5-123"


def test_activity_defaults_match_the_handover_contract() -> None:
    activity = Activity(
        name="新生见面会",
        date="2026-09-16",
        start="16:10",
        end="18:00",
        period="7-8",
        period_start=7,
        period_end=8,
        campus="仙林",
        campus_code="3",
    )
    # 这些默认值就是「CAC 说的不用让用户填」的东西：教学楼/教室随机、人数 30、类型团学活动
    assert activity.building == ""
    assert activity.room == ""
    assert activity.people == 30
    assert activity.people_source == "default"
    assert activity.borrow_type == "团学活动"


def test_application_json_roundtrip() -> None:
    app = Application(
        application_id=make_application_id("2026-09-16", "7-8", 1),
        packaged_at="2026-09-12T09:00:00+08:00",
        source=SourceRef(repo="g/r", doc_id=1, title="新生见面会"),
        activity=Activity(
            name="新生见面会",
            date="2026-09-16",
            start="16:10",
            end="18:00",
            period="7-8",
            period_start=7,
            period_end=8,
            campus="仙林",
            campus_code="3",
        ),
    )
    text = contract.dumps(app)
    assert text.endswith("\n")
    back = json.loads(text)
    assert back["schema_version"] == contract.SCHEMA_VERSION
    assert back["application_type"] == "classroom_borrow"
    assert Application.model_validate(back).activity.period == "7-8"


def test_unknown_fields_are_ignored_for_forward_compat() -> None:
    payload = {
        "application_id": "x",
        "packaged_at": "t",
        "source": {"repo": "g/r", "doc_id": 1, "future": 1},
        "activity": {
            "name": "n",
            "date": "d",
            "start": "s",
            "end": "e",
            "period": "1",
            "period_start": 1,
            "period_end": 1,
            "campus": "仙林",
            "campus_code": "3",
            "future_field": True,
        },
    }
    assert Application.model_validate(payload).activity.name == "n"


def test_json_schema_is_shipped_and_in_sync() -> None:
    """``docs/classroom-application.schema.json`` 必须与代码一致（交接物不能漂移）。"""
    expected = json.dumps(contract.json_schema(), ensure_ascii=False, indent=2) + "\n"
    assert SCHEMA_FILE.is_file(), (
        f"缺少 {SCHEMA_FILE}，跑 `yuque classroom schema --out {SCHEMA_FILE}`"
    )
    assert SCHEMA_FILE.read_text(encoding="utf-8") == expected


def test_json_schema_shape() -> None:
    schema = contract.json_schema()
    assert schema["$id"].endswith(contract.SCHEMA_VERSION)
    assert set(schema["required"]) == {"application_id", "packaged_at", "source", "activity"}
    props = schema["properties"]["activity"]["$ref"].rsplit("/", 1)[-1]
    assert props in schema["$defs"]
    assert "people_source" in schema["$defs"][props]["properties"]
