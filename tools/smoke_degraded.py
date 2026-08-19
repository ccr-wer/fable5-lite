import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.cli.main as m

# Drive stdin: discard any stale checkpoint (n), one task, then exit.
sys.stdin = io.StringIO("n\n重构登录系统模块\n\nexit\n")
m.main()
