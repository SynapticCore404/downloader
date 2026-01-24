from __future__ import annotations

import time
import threading
from typing import Any, Optional


class TTLCache:
    def __init__(self, default_ttl: float = 900.0):
        self._data: dict[str, tuple[Any, float]] = {}
        self._ttl = default_ttl
        self._lock = threading.Lock()

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expire = time.time() + (ttl if ttl is not None else self._ttl)
        with self._lock:
            self._data[key] = (value, expire)

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            value, expire = item
            if expire < now:
                del self._data[key]
                return None
            return value

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            to_delete = [k for k, (_, exp) in self._data.items() if exp < now]
            for k in to_delete:
                self._data.pop(k, None)
