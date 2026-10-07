"""tests 包路径锚：让 `python -m unittest discover -s tests` 也能找到项目根的模块
（server/checker/evidence/build_evidence）和同级测试模块。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))       # server.py / checker.py / evidence.py
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_evidence 等
sys.path.insert(0, str(ROOT / "tools"))  # build_evidence.py
