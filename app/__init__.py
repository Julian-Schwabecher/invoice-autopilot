"""Invoice Autopilot. Importing this package loads ./.env (repo root) into os.environ."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def load_env(path: Path = ENV_PATH) -> None:
    """Tiny KEY=VALUE loader. Never overrides a variable that is already set and non-empty."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key = key.strip()
        if key and not os.environ.get(key):
            os.environ[key] = _unquote(val)


load_env()
