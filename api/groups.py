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
from domain.org import load_prof_groups, resolve_users, rollup_groups
from domain.users import aggregate_users
from domain.views import job_views

router = APIRouter()


def _grouped(since_hours, running_only, level):
    """The shared pipeline both group routes run (plan commit 6).

    a. pinned window, b. the shared window sources in parallel, c. the
    memoized job view, d. the per-user aggregation, e. classification,
    f. the roll-up. Returns ``(pinned, conf, rows, mapping, coverage)``
    — rows ordered, both special rows always present.
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
    mapping, coverage = resolve_users((r["user"] for r in user_rows), conf)
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
