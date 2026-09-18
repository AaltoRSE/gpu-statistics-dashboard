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
    assert cache.job_utilization_key(24, None) == ("job_utilization", 24, None)
    assert cache.job_utilization_key(24, "alice") == ("job_utilization", 24, "alice")
    assert cache.job_utilization_key(24, None) != cache.job_utilization_key(72, None)
    assert cache.job_utilization_key(24, None) != cache.job_utilization_key(24, "alice")
    assert cache.job_vram_key(24) == ("job_vram", 24)
    assert cache.job_vram_key(24) != cache.job_vram_key(72)
    assert (cache.job_utilization_key(24, None)
            != cache.job_vram_key(24))
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


def test_invalidate_supersedes_an_in_flight_fetch():
    # A forced refresh that races a cold fetch must not JOIN the
    # pre-refresh flight (and re-cache its pre-refresh result): the
    # post-invalidate caller starts its own fresh fetch, the old
    # leader's result never lands in the store, and the old leader
    # completes without error (no InvalidStateError on a cancelled
    # future).
    c = cache.TtlCache()
    old_release = threading.Event()
    old_started = threading.Event()

    def old_fn():
        old_started.set()
        assert old_release.wait(timeout=5)
        return "stale"

    leader_result = {}

    def old_leader():
        try:
            leader_result["value"] = c.get_or_set("k", 60, old_fn)
        except Exception as e:  # noqa: BLE001 - surfaced below
            leader_result["error"] = e

    old_thread = threading.Thread(target=old_leader)
    old_thread.start()
    assert old_started.wait(timeout=5)

    # Forced refresh lands while the old fetch is still running.
    c.invalidate("k")

    fresh_calls = []

    def fresh_fn():
        fresh_calls.append(1)
        return "fresh"

    # The post-invalidate caller must run its OWN fn, not join old_fn.
    assert c.get_or_set("k", 60, fresh_fn) == "fresh"

    old_release.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive(), "the superseded leader hung"
    assert "error" not in leader_result, (
        "the superseded leader crashed: %r" % leader_result.get("error"))
    assert leader_result.get("value") == "stale"  # its own caller still sees it
    assert fresh_calls == [1]
    # The old leader's late publish must not overwrite the fresh value:
    # wait past any possible interleaving, then confirm the store holds
    # the FRESH generation.
    time.sleep(0.05)
    assert c.get_or_set("k", 60, lambda: "post") == "fresh"


def test_invalidate_ignores_warm_store_value():
    # invalidate() followed by get_or_set must REFETCH even if the
    # dropped entry's TTL had not expired.
    c = cache.TtlCache()
    assert c.get_or_set("k", 60, lambda: "old") == "old"
    c.invalidate("k")
    assert c.get_or_set("k", 60, lambda: "new") == "new"


def test_invalidate_keeps_single_flight_for_concurrent_refreshes():
    # Only ONE fresh fetch may run even when several callers arrive
    # after the invalidate (the first becomes the fresh leader; the
    # rest join its future — the marker is consumed immediately).
    c = cache.TtlCache()
    c.invalidate("k")
    calls = []
    call_lock = threading.Lock()
    release = threading.Event()

    def fn():
        with call_lock:
            calls.append(1)
        assert release.wait(timeout=5)
        return "fresh"

    results = [None] * 4

    def worker(i):
        results[i] = c.get_or_set("k", 60, fn)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert calls == [1]
    assert results == ["fresh"] * 4
