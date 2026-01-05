from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

class Config:
    """Simple JSON config loader with pseudo-comments support.

    - Keys that start with '_' are treated as comments by convention, but we simply ignore them when retrieving values.
    - All getters accept a *path* of keys: get("section", "key", "subkey", default=...)
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        raw = self.path.read_text(encoding="utf-8")
        self.data = json.loads(raw)

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for key in path:
            if not isinstance(cur, dict):
                return default
            if key not in cur:
                return default
            cur = cur.get(key)
        return cur if cur is not None else default

    def get_str(self, *path: str, default: str = "") -> str:
        v = self.get(*path, default=None)
        if v is None:
            return default
        return str(v)

    def get_int(self, *path: str, default: int = 0) -> int:
        v = self.get(*path, default=None)
        if v is None:
            return default
        try:
            # avoid treating booleans as ints
            if isinstance(v, bool):
                return default
            return int(v)
        except Exception:
            return default

    def get_int_list(self, *path: str, default: Optional[List[int]] = None) -> List[int]:
        if default is None:
            default = []
        v = self.get(*path, default=None)
        if v is None:
            return list(default)
        if isinstance(v, list):
            out: List[int] = []
            for item in v:
                try:
                    if isinstance(item, bool):
                        continue
                    out.append(int(item))
                except Exception:
                    continue
            return out
        # allow single value
        try:
            if isinstance(v, bool):
                return list(default)
            return [int(v)]
        except Exception:
            return list(default)
