import json

import pytest

from store import JsonStore, RequestNotFound


def test_next_id_increments_and_persists(tmp_path):
    path = tmp_path / "requests.json"
    store = JsonStore(path)
    assert store.next_id() == "PR-001"
    assert store.next_id() == "PR-002"
    # 換一個新的 store 讀同一個檔，流水號要接著跑
    assert JsonStore(path).next_id() == "PR-003"
    assert json.loads(path.read_text(encoding="utf-8"))["next_seq"] == 4


def test_clear_resets_sequence(tmp_path):
    store = JsonStore(tmp_path / "requests.json")
    store.next_id()
    store.save({"record": {"id": "PR-001"}})
    store.clear()
    assert store.all() == []
    assert store.next_id() == "PR-001"


def test_write_creates_missing_parent_dirs(tmp_path):
    store = JsonStore(tmp_path / "a" / "b" / "requests.json")
    assert store.next_id() == "PR-001"
    assert (tmp_path / "a" / "b" / "requests.json").exists()


def test_file_is_readable_utf8_with_indent(tmp_path):
    path = tmp_path / "requests.json"
    store = JsonStore(path)
    store.save({"record": {"id": "PR-001", "title": "伺服器"}})
    text = path.read_text(encoding="utf-8")
    # 中文不跳脫、縮排兩格，打開檔案就看得懂
    assert "伺服器" in text
    assert json.loads(text) == {"next_seq": 1, "requests": {"PR-001": {"record": {"id": "PR-001", "title": "伺服器"}}}}
    assert text.splitlines()[1].startswith('  "') and text.splitlines()[2].startswith('  "')


def test_get_unknown_request_carries_id(tmp_path):
    with pytest.raises(RequestNotFound) as exc_info:
        JsonStore(tmp_path / "requests.json").get("PR-999")
    assert str(exc_info.value) == "PR-999"
