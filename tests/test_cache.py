"""Tests for cache.py's TtlCache and key builders."""

import threading
import time

import pytest

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


def test_get_or_set_single_flights_concurrent_misses():
    calls = []
    call_lock = threading.Lock()
    release = threading.Event()

    def fn():
        with call_lock:
            calls.append(1)
        # Hold the leader here until every follower has had a chance to
        # join this same fetch instead of starting its own.
        assert release.wait(timeout=5)
        return "value"

    c = cache.TtlCache()
    results = [None] * 5

    def worker(i):
        results[i] = c.get_or_set("k", 60, fn)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.05)  # let all five reach get_or_set before releasing fn
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert calls == [1]  # fn ran exactly once
    assert results == ["value"] * 5


def test_get_or_set_failed_leader_lets_the_next_call_retry():
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return "value"

    c = cache.TtlCache()
    with pytest.raises(RuntimeError, match="boom"):
        c.get_or_set("k", 60, fn)
    # The failed fetch must not be cached, and must not be left
    # in-flight forever: the next call retries and succeeds.
    assert c.get_or_set("k", 60, fn) == "value"
    assert len(attempts) == 2


def test_get_or_set_concurrent_followers_all_see_the_leaders_exception():
    call_lock = threading.Lock()
    calls = []
    release = threading.Event()

    def fn():
        with call_lock:
            calls.append(1)
        assert release.wait(timeout=5)
        raise RuntimeError("boom")

    c = cache.TtlCache()
    errors = [None] * 5

    def worker(i):
        try:
            c.get_or_set("k", 60, fn)
        except RuntimeError as exc:
            errors[i] = str(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert calls == [1]  # fn still ran exactly once
    assert errors == ["boom"] * 5  # every caller saw the same failure
