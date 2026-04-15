import json
from pathlib import Path

_CONFIG_FILE = Path("config/default.json")

_DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 8900,
    "log_level": "info",
    "poll_interval": 60,
    "data_dir": "Data",
    "koreconversation_url": "http://localhost:8700",
}


def _load() -> dict:
    result = dict(_DEFAULTS)
    if not _CONFIG_FILE.exists():
        return result
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    result.update(raw)
    return result


cfg = _load()
