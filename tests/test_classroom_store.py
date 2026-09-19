"""本地持久化（state.json）单测。"""

from __future__ import annotations

import json
from pathlib import Path

from nju_yuque.classroom import store as store_mod
from nju_yuque.classroom.store import DocState, Meta, Store, default_outdir, open_store


def test_open_empty_store(tmp_path: Path) -> None:
    st, root = open_store(tmp_path / "out", "lqogh0/jsjysq")
    assert st.entries() == [] and st.repo == "lqogh0/jsjysq"
    assert root == tmp_path / "out"
    assert not (root / "state.json").exists()  # 打开不等于写入


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    st = Store(path, "g/r")
    st.put(DocState(doc_id=1, title="新生见面会", status=store_mod.STATUS_SUBMITTED))
    st.put(DocState(doc_id=2, title="读书会", status=store_mod.STATUS_REJECTED, problems=["x"]))
    st.meta.guide_doc_id = 99
    st.meta.rounds = 3
    st.save()

    again = Store(path, "g/r").load()
    assert again.repo == "g/r"
    assert again.meta.guide_doc_id == 99 and again.meta.rounds == 3
    assert again.get(1).status == store_mod.STATUS_SUBMITTED
    assert again.get(2).problems == ["x"]
    assert again.counts() == {
        store_mod.STATUS_SUBMITTED: 1,
        store_mod.STATUS_REJECTED: 1,
        store_mod.STATUS_UNRECOGNIZED: 0,
        "deleted": 0,
    }


def test_save_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    st = Store(path, "g/r")
    st.put(DocState(doc_id=1))
    st.save()
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == store_mod.STATE_VERSION


def test_corrupt_state_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    try:
        Store(path, "g/r").load()
    except ValueError as exc:
        assert "损坏" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("损坏的 state.json 应该报错而不是静默丢状态")


def test_unknown_fields_are_ignored(tmp_path: Path) -> None:
    """向前兼容：老版本写的多余字段不能让新版本崩。"""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "g/r",
                "docs": {"5": {"doc_id": 5, "title": "x", "future_field": 1}},
                "meta": {"guide_doc_id": 7, "unknown": "y"},
            }
        ),
        encoding="utf-8",
    )
    st = Store(path, "g/r").load()
    assert st.get(5).title == "x"
    assert st.meta.guide_doc_id == 7


def test_drop_and_notified_dedupe(tmp_path: Path) -> None:
    st = Store(tmp_path / "state.json", "g/r")
    st.put(DocState(doc_id=1))
    assert st.already_notified(1, "rejected", "abc") is False
    st.mark_notified(1, "rejected", "abc")
    assert st.already_notified(1, "rejected", "abc") is True
    assert st.already_notified(1, "rejected", "other") is False
    assert st.mark_notified(404, "rejected", "x") is None  # 没有记录时静默
    assert st.drop(1) is True and st.drop(1) is False


def test_next_seq_is_monotonic(tmp_path: Path) -> None:
    st = Store(tmp_path / "state.json", "g/r")
    assert [st.next_seq() for _ in range(3)] == [1, 2, 3]
    st.save()
    assert Store(tmp_path / "state.json", "g/r").load().next_seq() == 4


def test_meta_from_dict_ignores_unknown() -> None:
    assert Meta.from_dict({"seq": 2, "nope": 1}).seq == 2


def test_default_outdir_under_yuque_home(tmp_path: Path) -> None:
    root = default_outdir("lqogh0/jsjysq", home=tmp_path)
    assert root == tmp_path / "classroom" / "lqogh0_jsjysq"


def test_null_fields_are_tolerated(tmp_path: Path) -> None:
    """手改过的 state.json 里出现 null 也不能崩（常驻进程最怕这个）。"""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "repo": "g/r",
                "docs": {
                    "5": {
                        "doc_id": 5,
                        "title": None,
                        "status": None,
                        "problems": None,
                        "warnings": None,
                        "notified": None,
                        "raw_fields": None,
                        "activity": None,
                        "missing_rounds": None,
                    }
                },
                "meta": None,
            }
        ),
        encoding="utf-8",
    )
    st = Store(path, "g/r").load()
    state = st.get(5)
    assert state is not None
    assert state.title == "" and state.status == store_mod.STATUS_REJECTED
    assert state.problems == [] and state.notified == {} and state.raw_fields == {}
    assert state.missing_rounds == 0
    assert st.already_notified(5, "rejected", "x") is False
    st.mark_notified(5, "rejected", "x")
    assert st.counts()[store_mod.STATUS_REJECTED] == 1
    st.save()


def test_meta_with_wrong_type_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"version": 1, "repo": "g/r", "meta": "oops", "docs": {}}), encoding="utf-8"
    )
    st = Store(path, "g/r").load()
    assert st.meta.seq == 0 and st.meta.rounds == 0 and st.meta.guide_doc_id == 0
