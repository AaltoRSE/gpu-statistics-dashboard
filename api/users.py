"""Routes: GET /api/users and /api/users/{user}/priority."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import UserPriorityResponse, UsersResponse
from domain.common import window
from domain.priority import associations, headline_fairshare, pending_with_rank
from domain.users import aggregate_users
from domain.views import job_views

router = APIRouter()


@router.get("/api/users", response_model=UsersResponse)
def api_users(since_hours: float = Query(24, gt=0, le=720)):
    """Per-user GPU-activity aggregation over the window.

    Built from the same shared window sources the Jobs tab uses (plan
    §1/§2): the per-GPU utilization and VRAM % range queries, the
    scontrol node snapshot, and the running-only live snapshot all gather
    in parallel, then the memoized window view supplies the job rows —
    no sacct, so the list stays cheap.
    ``util_gpu_hours`` is the utilization-weighted GPU-hours
    (mean util x series hours), i.e. the same definition the Jobs tab's
    effective GPU-hours use before the allocation factor. Users are
    ordered by it (descending), ties broken by name.

    The sshare associations (300 s) and sacctmgr default accounts (1 h)
    gather alongside, for each row's headline ``fairshare``; a failed
    read nulls those figures and nothing else.
    """
    pinned = sources.pinned_window(since_hours)
    util, vram, live_snap, nodes, shares, defaults = sources.gather(
        lambda: sources.gpu_util(pinned),
        lambda: sources.vram_pct(pinned),
        lambda: sources.live_snapshot(),
        lambda: deps.route_cache.get_or_set(
            cache.scontrol_nodes_key(), 30, deps.show_nodes),
        sources.or_none(sources.fairshare),
        sources.or_none(sources.default_accounts),
    )
    node_types = gpu_groups.build_node_index(nodes)
    jobs_view = job_views(pinned, util, node_types, vram)

    users = aggregate_users(jobs_view, live_snap["live_ids"])
    for u in users:
        u["fairshare"], u["fairshare_account"] = headline_fairshare(
            (shares or {}).get(u["user"]), (defaults or {}).get(u["user"]))
    return {
        "window": window(pinned[0], pinned[1]),
        "count": len(users),
        "fairshare_available": shares is not None,
        "users": users,
    }


@router.get("/api/users/{user}/priority", response_model=UserPriorityResponse)
def api_user_priority(user: str):
    """One user's scheduling priority: every fairshare association
    (sshare, 300 s) with the primary one marked, their default account
    (sacctmgr, 1 h), and each of their pending jobs' priority per
    partition with its weighted factors and rank (sprio, 30 s).

    Each source degrades on its own (``*_available``); a user Slurm does
    not know answers with empty lists, not a 404, so an admin can look
    up any typed name.
    """
    shares, defaults, queue = sources.gather(
        sources.or_none(sources.fairshare),
        sources.or_none(sources.default_accounts),
        sources.or_none(sources.priorities),
    )
    # Slurm user names are lowercase; match case drift the way the Jobs
    # tab's user filter does.
    name = user.strip()
    if shares is not None and name not in shares:
        name = next((u for u in shares if u.casefold() == name.casefold()),
                    name)
    default_account = (defaults or {}).get(name)
    return {
        "user": name,
        "default_account": default_account,
        "fairshare_available": shares is not None,
        "associations": None if shares is None
        else associations(shares.get(name), default_account),
        "pending_available": queue is not None,
        "pending": None if queue is None else pending_with_rank(queue, name),
    }
