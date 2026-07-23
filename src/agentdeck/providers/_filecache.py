"""A path-keyed memoizer invalidated by a file's stat signature.

Both providers memoize cheap transcript reads (metadata, last event, context
tokens, delegation markers) so an idle transcript isn't re-parsed on every scan.
The stat→signature→lookup→recompute→store ceremony was identical across five
sites; this owns it once. A ``stat()`` failure yields the caller's default and
is not cached, so a transient error self-heals on the next read.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from pathlib import Path


def mtime_sig(path: Path) -> Hashable:
    """Freshness by modification time — for reads that only care that the file
    changed since the last parse (metadata, context tokens)."""
    return path.stat().st_mtime


def size_mtime_sig(path: Path) -> Hashable:
    """Freshness by (size, mtime_ns) — finer-grained, for tail reads that must
    catch a same-second rewrite (last event, delegation markers)."""
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


class FileCache[T]:
    """One path-keyed memo, invalidated when ``signature(path)`` changes."""

    def __init__(self, signature: Callable[[Path], Hashable]) -> None:
        self._signature = signature
        self._store: dict[str, tuple[Hashable, T]] = {}

    def get(self, path: Path, compute: Callable[[Path], T], default: T) -> T:
        """Cached ``compute(path)``, recomputed when the file's signature moves.

        Returns ``default`` without caching when the file cannot be ``stat``-ed."""
        try:
            signature = self._signature(path)
        except OSError:
            return default
        key = str(path)
        hit = self._store.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1]
        value = compute(path)
        self._store[key] = (signature, value)
        return value
