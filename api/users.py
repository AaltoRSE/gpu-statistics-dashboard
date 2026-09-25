"""Route: GET /api/users."""

from fastapi import APIRouter, Query

import cache
import deps
import gpu_groups
import sources
from api.schemas import UsersResponse
from domain.common import window
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

    users = aggregate_users(jobs_view, live_snap["live_ids"])
    return {
        "window": window(pinned[0], pinned[1]),
        "count": len(users),
        "users": users,
    }
