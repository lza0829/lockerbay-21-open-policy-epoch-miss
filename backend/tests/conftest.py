import os
import sys
import tempfile
from pathlib import Path

# 先固定数据目录再导入 app（db.py 在导入时解析 DATA_DIR）。
_DATA = Path(tempfile.mkdtemp(prefix="lockerbay-test-"))
os.environ.setdefault("DATA_DIR", str(_DATA))
os.environ.setdefault(
    "FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures")
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import db as db_mod
from app import seed as seed_mod


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """每个用例一份干净 SQLite 库（本地夹具，不触网）。"""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay.db")
    seed_mod.init_db()
    conn = db_mod.connect()
    try:
        yield conn
    finally:
        conn.close()
