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
