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
    assert cache.scontrol_jobs_key() == "scontrol_jobs"
    assert cache.scontrol_nodes_key() == "scontrol_nodes"
    assert cache.job_detail_key("1", 24) == ("jobdetail", "1", 24)
    assert cache.node_detail_key("gpu1", "job_start", 1000) == (
        "nodedetail", "gpu1", "job_start", 1000)
    # The collapsed progress key covers every fetch-affecting parameter:
    # the window is the only one (plan §3 — one dump serves the queue's
    # wait history and the VRAM enrichment, and partition/weight/running
    # are response-shape parameters that change no fetch).
    assert cache.vram_progress_key(24) == ("vram_progress", 24)
    assert cache.vram_progress_key(24) != cache.vram_progress_key(72)
    assert cache.group_members_key("laitos-t40106") == (
        "group_members", "laitos-t40106")


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


def test_peek_hits_unexpired_and_misses_missing_or_expired(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    c = cache.TtlCache()
    assert c.peek("k") == (False, None)  # absent
    c.get_or_set("k", 10, lambda: "value")
    assert c.peek("k") == (True, "value")
    now[0] += 11  # past the ttl
    assert c.peek("k") == (False, None)  # expired


def test_keyed_batch_cached_keys_skip_fetch():
    calls = []

    def fetch_missing(missing):
        calls.append(list(missing))
        return {k: [k] for k in missing}

    def ttl_for(key, value):
        return 60

    c = cache.KeyedBatchCache()
    assert c.get_batch(["a", "b"], fetch_missing, ttl_for) == \
        {"a": ["a"], "b": ["b"]}
    assert c.get_batch(["a", "b", "c"], fetch_missing, ttl_for) == \
        {"a": ["a"], "b": ["b"], "c": ["c"]}
    # The second call fetched only the uncached key; "a"/"b" came from
    # the store.
    assert calls == [["a", "b"], ["c"]]


def test_keyed_batch_overlapping_callers_join_inflight_keys():
    calls = []
    call_lock = threading.Lock()
    release = threading.Event()

    def fetch_missing(missing):
        with call_lock:
            calls.append(list(missing))
        assert release.wait(timeout=5)
        return {k: [k] for k in missing}

    def ttl_for(key, value):
        return 60

    c = cache.KeyedBatchCache()
    results = [None, None]

    def worker(i, keys):
        results[i] = c.get_batch(keys, fetch_missing, ttl_for)

    t1 = threading.Thread(target=worker, args=(0, ["a", "b"]))
    t1.start()
    time.sleep(0.05)  # let the first caller enter its held fetch
    t2 = threading.Thread(target=worker, args=(1, ["b", "c"]))
    t2.start()
    time.sleep(0.05)  # let the second caller reach get_batch
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # The second caller needed only "c": "b" was already in flight, so it
    # joined that future instead of starting its own fetch.
    assert calls == [["a", "b"], ["c"]]
    assert results == [{"a": ["a"], "b": ["b"]}, {"b": ["b"], "c": ["c"]}]


def test_keyed_batch_omitted_keys_resolve_none_and_retry_next_call():
    calls = []

    def fetch_missing(missing):
        calls.append(list(missing))
        # Simulate a failed batch: "b" never comes back.
        return {"a": ["a"]}

    c = cache.KeyedBatchCache()
    assert c.get_batch(["a", "b"], fetch_missing, lambda k, v: 60) == \
        {"a": ["a"], "b": None}
    # "b" was not negative-cached: the next call retries exactly it.
    assert c.get_batch(["a", "b"], fetch_missing, lambda k, v: 60) == \
        {"a": ["a"], "b": None}
    assert calls == [["a", "b"], ["b"]]


def test_keyed_batch_failed_leader_propagates_and_drops_inflight():
    calls = []

    def fetch_missing(missing):
        calls.append(list(missing))
        raise RuntimeError("boom")

    c = cache.KeyedBatchCache()
    with pytest.raises(RuntimeError, match="boom"):
        c.get_batch(["a"], fetch_missing, lambda k, v: 60)
    # Nothing cached, nothing left in flight: the next call retries
    # (and succeeds, via the recording-free replacement fetch).
    assert c.get_batch(["a"], lambda missing: {"a": ["a"]},
                       lambda k, v: 60) == {"a": ["a"]}
    assert calls == [["a"]]


def test_keyed_batch_per_key_ttl_is_honored(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])
    calls = []

    def fetch_missing(missing):
        calls.append(list(missing))
        return {k: k for k in missing}

    def ttl_for(key, value):
        # Terminal rows cache far longer than moving ones.
        return 3600 if key == "done" else 10

    c = cache.KeyedBatchCache()
    c.get_batch(["done", "running"], fetch_missing, ttl_for)
    assert calls == [["done", "running"]]
    now[0] += 11  # past the 10s ttl, within the 1h one
    c.get_batch(["done", "running"], fetch_missing, ttl_for)
    assert calls == [["done", "running"], ["running"]]


def test_new_key_builders_and_progress_store():
    assert cache.pinned_window_key(24) == ("pinned_window", 24)
    assert cache.window_source_key("gpu_util", 1, 2, 120) == \
        ("window_source", "gpu_util", 1, 2, 120)
    assert cache.window_source_key("vram_gb", 1, 2, 120) != \
        cache.window_source_key("gpu_util", 1, 2, 120)
    assert cache.snapshot_key() == ("snapshot",)
    assert cache.partition_views_key(1, 2, 120, "fp") == \
        ("part_views", 1, 2, 120, "fp")
    assert cache.partition_views_key(1, 2, 120, "fp") != \
        cache.partition_views_key(1, 2, 120, "other")
    assert cache.job_views_key(1, 2, 120, "fp", True) == \
        ("job_views", 1, 2, 120, "fp", True)
    # With-VRAM and without-VRAM job rows memoize apart: a caller that
    # skipped the VRAM source must not read the other's vram_avg.
    assert cache.job_views_key(1, 2, 120, "fp", True) != \
        cache.job_views_key(1, 2, 120, "fp", False)
    assert cache.sacct_window_key(24) == ("sacct_window", 24)
    assert cache.day_chunk_key("2026-09-23T00:00:00") == \
        ("sacct_day_chunk", "2026-09-23T00:00:00")
    # The Groups classification's progress key keeps the fetch-affecting
    # parameters (window + running_only, which selects the owners) and
    # drops level (an in-process roll-up, the same collapse as
    # vram_progress_key's partition/running_only).
    assert cache.groups_progress_key(24, False) == \
        ("groups_progress", 24, False)
    assert cache.groups_progress_key(24, False) != \
        cache.groups_progress_key(72, False)
    assert cache.groups_progress_key(24, False) != \
        cache.groups_progress_key(24, True)
    # The directory-phase identities: stable, and distinct per input.
    assert cache.gid_name_key(1010) == ("gid_name", 1010)
    assert cache.gid_name_key(1010) != cache.gid_name_key(1011)
    assert cache.prof_groups_index_key("/p/c.conf", 7.0) == \
        ("prof_groups_index", "/p/c.conf", 7.0)
    assert cache.prof_groups_index_key("/p/c.conf", 7.0) != \
        cache.prof_groups_index_key("/p/c.conf", 8.0)
    assert cache.groups_classification_key("/p/c.conf", 7.0, ["b", "a"]) == \
        ("groups_classified", "/p/c.conf", 7.0, ("a", "b"))
    assert cache.groups_classification_key("/p/c.conf", 7.0, ["a"]) != \
        cache.groups_classification_key("/p/c.conf", 7.0, ["a", "b"])
    # The one store: domain.partitions's alias is the same dict object.
    import domain.partitions
    assert domain.partitions.progress_store is cache.progress_store
