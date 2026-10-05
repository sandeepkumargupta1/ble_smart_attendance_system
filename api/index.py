import sys
import os
from pathlib import Path

# Add project root and Backend to sys.path
root_dir = Path(__file__).resolve().parent.parent
backend_dir = root_dir / "Backend"

for p in (str(root_dir), str(backend_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("VERCEL", "1")

from Backend.main import app as _app

app = _app
application = _app
handler = _app
