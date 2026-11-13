import json
import os
import urllib.request
from pathlib import Path

config = json.loads((Path(os.environ.get("DUSHA_HOME", "/data")) / "config.json").read_text())
urllib.request.urlopen(f"http://127.0.0.1:{config.get('port', 8765)}/health", timeout=5)
