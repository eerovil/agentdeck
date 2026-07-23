"""Unit tests for the shared FileCache memoizer."""

import json

from agentdeck.providers._filecache import FileCache, mtime_sig, size_mtime_sig


def test_miss_computes_once_then_hits_without_recomputing(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("a\n")
    calls = []

    def compute(p):
        calls.append(p)
        return p.read_text()

    cache: FileCache[str] = FileCache(size_mtime_sig)
    assert cache.get(path, compute, "default") == "a\n"
    assert cache.get(path, compute, "default") == "a\n"  # second call is a hit
    assert len(calls) == 1  # compute ran exactly once


def test_signature_change_recomputes(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("one\n")
    cache: FileCache[str] = FileCache(size_mtime_sig)
    assert cache.get(path, lambda p: p.read_text(), "") == "one\n"
    # A different size (and mtime_ns) invalidates the entry.
    path.write_text("two two\n")
    assert cache.get(path, lambda p: p.read_text(), "") == "two two\n"


def test_missing_file_returns_default_and_is_not_cached(tmp_path):
    path = tmp_path / "nope.jsonl"
    calls = []

    def compute(p):
        calls.append(p)
        return "computed"

    cache: FileCache[str] = FileCache(mtime_sig)
    assert cache.get(path, compute, "fallback") == "fallback"  # stat fails
    assert calls == []  # compute never ran
    # Once the file appears, the next read recomputes (nothing was cached).
    path.write_text("x\n")
    assert cache.get(path, compute, "fallback") == "computed"
    assert len(calls) == 1


def test_signature_functions(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text(json.dumps({"a": 1}))
    assert isinstance(mtime_sig(path), float)
    sig = size_mtime_sig(path)
    assert isinstance(sig, tuple) and len(sig) == 2
