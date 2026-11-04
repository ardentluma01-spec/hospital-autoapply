import os
import sys
import tempfile
from pathlib import Path

os.environ["AUTOAPPLY_DATA"] = tempfile.mkdtemp(prefix="autoapply-test-")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("DASHBOARD_PASSWORD", "pw")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
