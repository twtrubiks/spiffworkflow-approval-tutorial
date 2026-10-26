"""
用一個 JSON 檔模擬資料庫。

實務上 workflow 的序列化結果會存在資料表的欄位裡，這裡改存檔案，方便直接打開來看。
"""
import json
import os
import threading
from pathlib import Path


class RequestNotFound(Exception):
    """找不到單據"""


class JsonStore:

    def __init__(self, path):
        self.path = Path(path)
        # 會簽時兩個人可能同時按核准，各自「讀出 → 操作 → 存回」會互相蓋掉，所以整段操作要上鎖
        self.lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {"next_seq": 1, "requests": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, db):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先寫暫存檔再換名，寫到一半中斷也不會留下壞掉的檔案
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def next_id(self):
        with self.lock:
            db = self._read()
            seq = db["next_seq"]
            db["next_seq"] = seq + 1
            self._write(db)
            return f"PR-{seq:03d}"

    def get(self, request_id):
        try:
            return self._read()["requests"][request_id]
        except KeyError:
            raise RequestNotFound(request_id) from None

    def all(self):
        return list(self._read()["requests"].values())

    def save(self, data):
        with self.lock:
            db = self._read()
            db["requests"][data["record"]["id"]] = data
            self._write(db)

    def clear(self):
        with self.lock:
            self._write({"next_seq": 1, "requests": {}})
