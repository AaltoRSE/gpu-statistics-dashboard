"""The refactor's core guarantees, verified end to end: the windowed
routes share ONE upstream fetch per source (exact call counts), and each
route's independent sources are gathered concurrently.

Run: .venv/bin/python -m pytest tests/test_shared_fetch.py -q

The fixtures and canned data are reused from test_app (FakeProm and the
``fake_prom``/``client`` fixtures, which patch everything on deps); the
counting and gating layers here wrap those stubs without touching
application code.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from test_app import SACCT, FakeProm  # noqa: E402

import deps  # noqa: E402
import sources  # noqa: E402

# The windowed/tab endpoints, all read for the same 24 h window. The
# groups routes belong here deliberately: their only external read
# beyond the shared sources is per-user NSS (uncounted, cached), so
# their presence in this list is what proves the Groups tab adds no
# Prometheus/sacct/scontrol fetch of its own.
WINDOW_HITS = [
    ("/api/jobs", {"since_hours": 24}),
    ("/api/users", {"since_hours": 24}),
    ("/api/jobs", {"since_hours": 24, "user": "alice"}),
    ("/api/groups", {"since_hours": 24}),
    ("/api/groups/unit:T40106/users", {"since_hours": 24}),
    ("/api/partitions", {"since_hours": 24}),
    ("/api/partitions/queue", {"since_hours": 24}),
    ("/api/partitions/vram", {"since_hours": 24}),
    ("/api/nodes", {}),
]

# Query fragments that identify one window source inside a PromQL query.
UTIL_FRAG = "slurm_job_utilization_gpu"
VRAM_PCT_FRAG = "avg by (slurmjobid, instance, gpu)"
VRAM_GB_FRAG = "1073741824"
NODE_VRAM_FRAG = "avg by (instance)"

GATE_TIMEOUT = 5.0


class SlurmCounts:
    """Invocation counts of the deps-level Slurm fetchers, layered over
    the fake_prom fixture's stubs. Prometheus-side calls are counted
    from FakeProm.calls; these are the sacct/scontrol seams."""

    def __init__(self):
        self.calls = {"show_nodes": 0, "show_jobs": 0,
                      "sacct_allocations": 0, "sacct_jobs_resilient": 0}
        self.chunk_bounds = []  # (start_iso, end_iso) per dump chunk
        self.resilient_ids = []  # one sorted id list per resilient call


@pytest.fixture()
def slurm_counts(fake_prom, monkeypatch):
    counts = SlurmCounts()
    real_alloc = deps.sacct_allocations
    real_resilient = deps.sacct_jobs_resilient
    real_show_jobs = deps.show_jobs
    real_show_nodes = deps.show_nodes

    def alloc(start_iso, end_iso, partitions):
        counts.calls["sacct_allocations"] += 1
        counts.chunk_bounds.append((start_iso, end_iso))
        return real_alloc(start_iso, end_iso, partitions)

    def resilient(ids, start_iso=None, **kw):
        counts.calls["sacct_jobs_resilient"] += 1
        counts.resilient_ids.append(sorted(str(i) for i in ids))
        return real_resilient(ids, start_iso=start_iso, **kw)

    def show_jobs():
        counts.calls["show_jobs"] += 1
        return real_show_jobs()

    def show_nodes():
        counts.calls["show_nodes"] += 1
        return real_show_nodes()

    monkeypatch.setattr(deps, "sacct_allocations", alloc)
    monkeypatch.setattr(deps, "sacct_jobs_resilient", resilient)
    monkeypatch.setattr(deps, "show_jobs", show_jobs)
    monkeypatch.setattr(deps, "show_nodes", show_nodes)
    return counts


def _hit_all(client):
    for path, params in WINDOW_HITS:
        r = client.get(path, params=params)
        assert r.status_code == 200, (path, r.status_code, r.text)


def test_window_routes_share_one_fetch_per_source(client, fake_prom,
                                                  slurm_counts):
    """Every tab read for one window: three range queries, two instant
    queries, one scontrol snapshot pair, one day-chunked sacct dump, and
    one per-ID fallback batch — then a warm second pass of all seven
    routes issues no upstream query at all."""
    _hit_all(client)

    ranges = [q for t, q in fake_prom.calls if t == "range"]
    instants = [q for t, q in fake_prom.calls if t == "instant"]
    # One range fetch per window source — NOT one per route/tab: the jobs
    # list, the single-user jobs refetch, the users aggregation, the
    # partition summary, the queue's utilization groups and the VRAM
    # chart all read the same three cached window sources (plan §2).
    assert len(ranges) == 3
    assert set(ranges) == {sources._GPU_UTIL_QUERY, sources._VRAM_PCT_QUERY,
                           sources._VRAM_GB_QUERY}
    # live_snapshot's pair serves /api/users, /api/partitions and
    # /api/nodes (30 s snapshot cache).
    assert len(instants) == 2
    assert any(UTIL_FRAG in q for q in instants)
    assert any(NODE_VRAM_FRAG in q for q in instants)
    # scontrol: one show_nodes (the scontrol_nodes snapshot every route
    # and the sacct dump's partition list read) and one show_jobs (the
    # jobs list's active-job enrichment).
    assert slurm_counts.calls["show_nodes"] == 1
    assert slurm_counts.calls["show_jobs"] == 1
    # One sacct window dump, shared by the queue's wait history and the
    # VRAM enrichment: one deps.sacct_allocations call per Helsinki day
    # chunk of the 24 h window — the two edge chunks around midnight.
    assert slurm_counts.calls["sacct_allocations"] == 2
    assert slurm_counts.chunk_bounds == [
        ("2026-08-29T17:26:40", "2026-08-30T00:00:00"),
        ("2026-08-30T00:00:00", "2026-08-30T17:26:40"),
    ]
    # The per-ID fallback ran once, for exactly the jobs-list IDs: the
    # VRAM records were enriched by the shared dump (no fallback there).
    assert slurm_counts.calls["sacct_jobs_resilient"] == 1
    assert slurm_counts.resilient_ids == [sorted(SACCT)]

    # Warm pass: every endpoint again, same window — everything served
    # from the route/sacct caches, zero additional upstream calls.
    ranges_before = len(ranges)
    instants_before = len(instants)
    calls_before = dict(slurm_counts.calls)
    _hit_all(client)
    assert [q for t, q in fake_prom.calls if t == "range"] == ranges
    assert len(ranges) == ranges_before
    assert len([q for t, q in fake_prom.calls if t == "instant"]) == \
        instants_before
    assert slurm_counts.calls == calls_before
    assert len(slurm_counts.resilient_ids) == 1


# ---- overlap (gather fan-out) ----------------------------------------

class OverlapGate:
    """A bounded-wait barrier proving fetches overlap.

    Participants are named and grouped into phases: entering a
    participant blocks (bounded) until every participant of its phase
    has entered, then all proceed together. If the route instead ran
    its fetches sequentially, the late participant never enters while
    its phase-mates wait, each wait expires after ``timeout`` seconds
    and records the miss — the test asserts ``timeouts`` is empty, so a
    sequential route fails the test rather than deadlocking the suite.
    """

    def __init__(self, *phases, timeout=GATE_TIMEOUT):
        self._timeout = timeout
        self._events = {name: threading.Event()
                        for phase in phases for name in phase}
        self._phases = [set(phase) for phase in phases]
        self.timeouts = []

    def enter(self, name):
        self._events[name].set()
        deadline = time.monotonic() + self._timeout
        for phase in self._phases:
            if name not in phase:
                continue
            for peer in phase:
                remaining = deadline - time.monotonic()
                if not self._events[peer].wait(timeout=max(remaining, 0.0)):
                    self.timeouts.append(peer)


class GatedProm(FakeProm):
    """FakeProm that enters an overlap-gate participant per matching
    query, then answers from the ordinary canned fixture data. Rules are
    ``(method, fragment, participant)`` triples; the first fragment found
    in the query selects the participant to enter."""

    def __init__(self, gate, rules):
        super().__init__()
        self.gate = gate
        self.rules = rules

    def _gate(self, method, query):
        for method_, fragment, name in self.rules:
            if method == method_ and fragment in query:
                self.gate.enter(name)
                return

    def query_range(self, query, start, end, step):
        self._gate("range", query)
        return super().query_range(query, start, end, step)

    def query_instant(self, query, time=None):
        self._gate("instant", query)
        return super().query_instant(query, time)


def _gated_fn(gate, name, fn):
    """Wrap a deps-level fetcher so calling it enters a gate participant."""

    def wrapper(*args, **kwargs):
        gate.enter(name)
        return fn(*args, **kwargs)

    return wrapper


def test_jobs_route_overlaps_util_and_vram_fetches(client, fake_prom,
                                                   monkeypatch):
    gate = OverlapGate(("util_range", "vram_range"))
    monkeypatch.setattr(deps, "get_prom", lambda: GatedProm(gate, [
        ("range", UTIL_FRAG, "util_range"),
        ("range", VRAM_PCT_FRAG, "vram_range"),
    ]))
    r = client.get("/api/jobs", params={"since_hours": 24})
    assert r.status_code == 200
    assert gate.timeouts == [], \
        "the jobs list's two window sources must be gathered in parallel"


def test_partitions_route_overlaps_window_fetch_and_live_snapshot(
        client, fake_prom, monkeypatch):
    gate = OverlapGate(("util_range", "live_instant"))
    monkeypatch.setattr(deps, "get_prom", lambda: GatedProm(gate, [
        ("range", UTIL_FRAG, "util_range"),
        ("instant", UTIL_FRAG, "live_instant"),
    ]))
    r = client.get("/api/partitions", params={"since_hours": 24})
    assert r.status_code == 200
    assert gate.timeouts == [], \
        "the partition summary and the live snapshot must be gathered " \
        "in parallel"


def test_partitions_vram_route_overlaps_three_sources(client, fake_prom,
                                                      monkeypatch):
    real_alloc = deps.sacct_allocations
    gate = OverlapGate(("util_range", "vram_gb_range", "sacct_chunks"))
    monkeypatch.setattr(deps, "get_prom", lambda: GatedProm(gate, [
        ("range", UTIL_FRAG, "util_range"),
        ("range", VRAM_GB_FRAG, "vram_gb_range"),
    ]))
    monkeypatch.setattr(
        deps, "sacct_allocations",
        _gated_fn(gate, "sacct_chunks", real_alloc))
    r = client.get("/api/partitions/vram", params={"since_hours": 24})
    assert r.status_code == 200
    assert gate.timeouts == [], \
        "utilization, VRAM GB and the sacct window dump must be gathered " \
        "in parallel"


def test_users_route_gathers_four_sources(client, fake_prom, monkeypatch):
    real_nodes = deps.show_nodes
    # Phase 1: the three direct thunks (util range, VRAM % range, the
    # scontrol snapshot) must all be entered together. Phase 2: the live
    # snapshot's two instant queries — they run after the snapshot's
    # scontrol index resolves, i.e. after phase 1 releases — must also
    # overlap each other. Gating them into one phase would deadlock by
    # construction: the instants cannot start before show_nodes returns.
    gate = OverlapGate(("util_range", "vram_range", "scontrol_nodes"),
                       ("live_util_instant", "live_vram_instant"))
    monkeypatch.setattr(deps, "get_prom", lambda: GatedProm(gate, [
        ("range", UTIL_FRAG, "util_range"),
        ("range", VRAM_PCT_FRAG, "vram_range"),
        ("instant", UTIL_FRAG, "live_util_instant"),
        ("instant", NODE_VRAM_FRAG, "live_vram_instant"),
    ]))
    monkeypatch.setattr(
        deps, "show_nodes", _gated_fn(gate, "scontrol_nodes", real_nodes))
    r = client.get("/api/users", params={"since_hours": 24})
    assert r.status_code == 200
    assert gate.timeouts == [], \
        "the users route's four sources must be gathered in parallel"


def test_groups_route_gathers_four_sources(client, fake_prom, monkeypatch):
    # The Groups pipeline shares the users route's exact source shape —
    # its gather must overlap the same way (the NSS lookups run after
    # the responses land and are not gated here).
    real_nodes = deps.show_nodes
    gate = OverlapGate(("util_range", "vram_range", "scontrol_nodes"),
                       ("live_util_instant", "live_vram_instant"))
    monkeypatch.setattr(deps, "get_prom", lambda: GatedProm(gate, [
        ("range", UTIL_FRAG, "util_range"),
        ("range", VRAM_PCT_FRAG, "vram_range"),
        ("instant", UTIL_FRAG, "live_util_instant"),
        ("instant", NODE_VRAM_FRAG, "live_vram_instant"),
    ]))
    monkeypatch.setattr(
        deps, "show_nodes", _gated_fn(gate, "scontrol_nodes", real_nodes))
    r = client.get("/api/groups", params={"since_hours": 24})
    assert r.status_code == 200
    assert gate.timeouts == [], \
        "the groups route's four sources must be gathered in parallel"


def test_gates_catch_sequential_fetches():
    # Sanity check of the gate itself: a participant that never runs
    # (the sequential-fetcher signature) times its phase-mates out and is
    # recorded — the property every overlap test above relies on.
    gate = OverlapGate(("a", "b"), timeout=0.05)
    gate.enter("a")
    assert gate.timeouts == ["b"]
    gate.enter("b")  # late arrival: recorded miss is not undone
    assert gate.timeouts == ["b"]
    clean = OverlapGate(("a", "b"), timeout=0.05)
    threads = [threading.Thread(target=clean.enter, args=(name,))
               for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert clean.timeouts == []  # concurrent entrants release each other
