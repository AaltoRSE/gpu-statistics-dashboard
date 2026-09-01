"""Tests for cache.py's TtlCache and key builders."""

import cache


def test_get_or_set_calls_fn_once_within_ttl():
    calls = []

    def fn():
        calls.append(1)
        return "value"

    c = cache.TtlCache()
    assert c.get_or_set("k", 60, fn) == "value"
    assert c.get_or_set("k", 60, fn) == "value"
    assert len(calls) == 1


def test_get_or_set_refetches_after_expiry(monkeypatch):
    calls = []

    def fn():
        calls.append(1)
        return len(calls)

    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    c = cache.TtlCache()
    assert c.get_or_set("k", 10, fn) == 1
    now[0] += 11  # past the 10s ttl
    assert c.get_or_set("k", 10, fn) == 2
    assert len(calls) == 2


def test_invalidate_drops_only_the_named_keys():
    c = cache.TtlCache()
    c.get_or_set("a", 60, lambda: "a-value")
    c.get_or_set("b", 60, lambda: "b-value")
    c.invalidate("a")
    calls = []
    assert c.get_or_set("a", 60, lambda: calls.append(1) or "a-again") == "a-again"
    assert len(calls) == 1
    calls_b = []
    assert c.get_or_set("b", 60, lambda: calls_b.append(1) or "b-value") == "b-value"
    assert len(calls_b) == 0  # "b" was untouched by invalidating "a"


def test_clear_drops_everything():
    c = cache.TtlCache()
    c.get_or_set("a", 60, lambda: "a")
    c.clear()
    calls = []
    c.get_or_set("a", 60, lambda: calls.append(1))
    assert len(calls) == 1


def test_oversized_store_clears_on_next_write():
    # Eviction only fires once size *exceeds* max_size at write time, so
    # crossing it takes one more insert than max_size — matching what
    # both prior hand-rolled caches did (`if len(_cache) > N: clear()`).
    c = cache.TtlCache(max_size=1)
    c.get_or_set("a", 60, lambda: "a")
    c.get_or_set("b", 60, lambda: "b")  # size 1 -> not > 1 yet, no clear
    c.get_or_set("c", 60, lambda: "c")  # size 2 -> > 1: clears, then inserts "c"
    calls = []
    c.get_or_set("a", 60, lambda: calls.append(1) or "a-again")
    assert len(calls) == 1  # "a" was evicted by the clear, so fn ran again


def test_key_builders_are_stable_and_distinct():
    assert cache.job_window_key(24, True, None) == ("jobs", 24, True, None)
    assert cache.job_window_key(24, True, "alice") == ("jobs", 24, True, "alice")
    assert cache.job_window_key(24, True, None) != cache.job_window_key(72, True, None)
    assert cache.sacct_key(["2", "1"]) == cache.sacct_key(["1", "2"])
    assert cache.scontrol_jobs_key() == "scontrol_jobs"
    assert cache.scontrol_nodes_key() == "scontrol_nodes"
    assert cache.job_detail_key("1", 24) == ("jobdetail", "1", 24)
    assert cache.partition_window_key(24, False) == ("parts", 24, False)
    assert cache.vram_key(24, True) == ("vram_gb", 24, True)
    assert cache.node_current_key() == "node_current"
    assert cache.node_detail_key("gpu1", "job_start", 1000) == (
        "nodedetail", "gpu1", "job_start", 1000)
