"""
Tiny TTL cache. Process-local; good enough for App Engine F1 with one worker.
If you scale out, swap for memcache / redis without changing call sites.
"""
import threading
import time
from typing import Any, Callable


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get_or_load(self, key: str, loader: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                expires_at, value = entry
                if expires_at > now:
                    return value
        # Load outside the lock so slow loaders don't block other keys
        value = loader()
        with self._lock:
            self._store[key] = (now + self._ttl, value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)
