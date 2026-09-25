"""Routes: GET /api/groups and GET /api/groups/{group_id}/users.

The Groups tab's endpoints: per-professor-group / department / school
GPU efficiency over the window, classified from each job owner's NSS
groups (``groups <user>`` on this host) and prof_groups.conf names. The
pipeline shares every window source with the other tabs (plan §1/§2) —
the only new external reads are per-user NSS (cached per user for 24 h,
1 h for a user the directory does not know) and the configured groups'
member lists (cached per group for 24 h), so a repeat request makes
zero extra directory calls and /api/users + /api/groups in one window
still add no Prometheus query.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import GroupMembersResponse, GroupsResponse
from domain.common import window
from domain.org import (
    _index_cached,
    load_prof_groups,
    resolve_users,
    rollup_groups,
)
from domain.users import aggregate_users
from domain.views import job_views

router = APIRouter()


def _classified(since_hours, running_only, usernames, conf):
    """The classification half of the pipeline, single-flighted and
    published (plan §directory).

    A window, level or running change fires /api/groups and the open
    drill-down /api/groups/{id}/users together, and both run
    resolve_users. Without single-flight, two publishers would write the
    same progress key and the first to finish would pop it while the
    other is still running — doubling the NSS work. So the whole
    classification (index build + per-user lookups) runs once under one
    directory-cache key, and only the get_or_set leader publishes
    progress: a follower that joins the Future never touches the key.
    The result is memoized 60 s, so a level change within the TTL is an
    instant hit (level is response-shape only). A partial failure (any
    coverage.failed) is never memoized — the next request retries it —
    while a total outage raises DirectoryError out of get_or_set, which
    caches nothing.
    """
    pkey = cache.groups_progress_key(since_hours, running_only)

    def run():
        last = {}

        def publish(state):
            last["state"] = state
            cache.progress_store[pkey] = state

        try:
            index = _index_cached(conf, progress=publish)
            return resolve_users(usernames, conf, index=index,
                                 progress=publish)
        finally:
            # Pop only our own entry — never a concurrent leader's: the
            # identity check keeps a follower's finally (or a late
            # publish) from erasing another run's live state.
            if cache.progress_store.get(pkey) is last.get("state"):
                cache.progress_store.pop(pkey, None)

    if conf.get("_source") is None:  # hand-built confs (tests): uncached
        return run()
    key = cache.groups_classification_key(*conf["_source"], usernames)
    mapping, coverage = deps.directory_cache.get_or_set(key, 60, run)
    if coverage["failed"]:
        deps.directory_cache.invalidate(key)  # errors are never cached
    return mapping, coverage


def _grouped(since_hours, running_only, level):
    """The shared pipeline both group routes run (plan commit 6).

    a. pinned window, b. the shared window sources in parallel, c. the
    memoized job view, d. the per-user aggregation, e. the single-
    flighted classification, f. the roll-up. Returns ``(pinned, conf,
    rows, mapping, coverage)`` — rows ordered, the Unaffiliated row
    always present.
    """
    pinned = sources.pinned_window(since_hours)
    util, vram, live_snap, nodes = sources.gather(
        lambda: sources.gpu_util(pinned),
        lambda: sources.vram_pct(pinned),
        lambda: sources.live_snapshot(),
        lambda: deps.route_cache.get_or_set(
            cache.scontrol_nodes_key(), 30, deps.show_nodes),
    )
    node_types = gpu_groups.build_node_index(nodes)
    jobs_view = job_views(pinned, util, node_types, vram)
    live = live_snap["live_ids"]
    if running_only:
        # The live-ID filter is in process over the shared unfiltered
        # fetch — the same substitution every running-only view makes.
        jobs_view = [j for j in jobs_view if j["jobid"] in live]
    user_rows = aggregate_users(jobs_view, live)
    conf = load_prof_groups()
    mapping, coverage = _classified(
        since_hours, running_only, [r["user"] for r in user_rows], conf)
    rows = rollup_groups(user_rows, mapping, jobs_view, pinned[2],
                         level=level, conf=conf)
    return pinned, conf, rows, mapping, coverage


def _schools(conf):
    return [
        {"code": code, "short": s["short"], "full": s["full"]}
        for code, s in sorted(conf["schools"].items())
    ]


@router.get("/api/groups", response_model=GroupsResponse)
def api_groups(
    since_hours: float = Query(24, gt=0, le=720),
    running_only: bool = Query(False),
    level: Literal["group", "department"] = Query("group"),
):
    """GPU efficiency per professor research group over the window.

    Roll-up of the Users tab's per-user aggregates by professor group
    (level=group, the AD unit a prof_groups.conf row names) or
    department (level=department). No new upstream fetch: the window
    sources, job view and per-user rows are the shared ones; only the
    per-user NSS classification is added, cached per user.
    """
    pinned, conf, rows, _, coverage = _grouped(
        since_hours, running_only, level)
    return {
        "window": window(pinned[0], pinned[1]),
        "level": level,
        "schools": _schools(conf),
        "coverage": coverage,
        "count": len(rows),
        "groups": rows,
    }


@router.get("/api/groups/progress")
def api_groups_progress(
    since_hours: float = Query(24, gt=0, le=720),
    running_only: bool = Query(False),
    level: str = "group",  # accepted, ignored: the poll reuses the data
                           # request's query string
):
    """Batched directory progress for the Groups classification (the
    same poll shape the Partitions tabs use).

    While a classification's directory phase runs, its leader publishes
    ``{"phase": "index" | "users", "done", "total", "failed_batches"}``
    under the (since_hours, running_only) key and this route returns the
    latest state; between phases and after the phase finishes the key is
    popped and this returns JSON null, which hands the chip back to its
    base label. Declared before the ``{group_id}`` route so "progress"
    is never read as a group id.
    """
    return cache.progress_store.get(
        cache.groups_progress_key(since_hours, running_only))


@router.get("/api/groups/{group_id}/users", response_model=GroupMembersResponse)
def api_group_users(
    group_id: str,
    since_hours: float = Query(24, gt=0, le=720),
    running_only: bool = Query(False),
    level: Literal["group", "department"] = Query("group"),
):
    """The drill-down: every member of one roll-up row, with their own
    classification (group, membership kind, department, extra groups).
    Unknown group ids are a 404, not an empty member list."""
    pinned, _, rows, _, _ = _grouped(since_hours, running_only, level)
    for row in rows:
        if row["group_id"] == group_id:
            return {
                "group_id": group_id,
                "group_name": row["group_name"],
                "level": level,
                "window": window(pinned[0], pinned[1]),
                "count": len(row["members"]),
                "users": row["members"],
            }
    raise HTTPException(404, "no such group in this window/level: "
                             "%s" % group_id)
