"""公共工具函数"""

from __future__ import annotations

import json
from typing import Any


def parse_json_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}
