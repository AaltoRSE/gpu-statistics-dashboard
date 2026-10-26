"""Routes: GET /api/status/live, /api/status/history and its progress
poll — the Status tab's cluster overview."""

from fastapi import APIRouter, Query

import cache
import deps
import sources
from api.schemas import StatusHistoryResponse, StatusLiveResponse
from domain.status import live_status
from slurm import SlurmError

router = APIRouter()


def _or_none(fetch):
    """A source thunk that reads as None instead of raising SlurmError:
    each live source degrades on its own."""
    def run():
        try:
            return fetch()
        except SlurmError:
            return None
    return run


@router.get("/api/status/live", response_model=StatusLiveResponse)
def api_status_live(refresh: bool = Query(False)):
    """Occupancy, node states, running and pending jobs right now.

    Three independent sources, read in parallel: the scontrol node and
    job snapshots (30 s, shared with the other tabs) and squeue's
    pending jobs (30 s). One failing source nulls only the figures that
    need it. ``refresh`` drops all three cached snapshots first.
    """
    if refresh:
        deps.route_cache.invalidate(cache.scontrol_nodes_key(),
                                    cache.scontrol_jobs_key(),
                                    cache.squeue_pending_key())
    nodes, jobs, pending = sources.gather(
        _or_none(sources.scontrol_nodes),
        _or_none(sources.scontrol_jobs),
        _or_none(sources.pending_jobs),
    )
    return live_status(nodes, jobs, pending, deps.now())


@router.get("/api/status/history", response_model=StatusHistoryResponse)
def api_status_history(refresh: bool = Query(False)):
    """The past seven days of cluster-wide job history, hour by hour,
    with past-24-h and past-7-days summaries split into CPU and GPU jobs.

    Always 200: when sacct cannot be read at all the body says
    ``available: false`` (never an empty week); a partial read is
    disclosed in ``coverage``. ``refresh`` re-assembles the history —
    its two edge chunks re-fetch, while full past days stay cached.
    """
    if refresh:
        deps.route_cache.invalidate(cache.status_history_key())
    try:
        history = sources.status_history(
            progress_key=cache.status_progress_key())
    except SlurmError:
        return {"available": False}
    try:
        nodes = sources.scontrol_nodes()
        capacity = {"cpus": sum(n.get("cpus") or 0 for n in nodes),
                    "gpus": sum(n.get("gpus") or 0 for n in nodes)}
    except SlurmError:
        capacity = None
    return {**history, "capacity": capacity}


@router.get("/api/status/history/progress")
def api_status_history_progress():
    """Batched progress of an in-flight history fetch: ``{done, total,
    failed_batches}`` per day chunk, or JSON null when nothing is in
    flight (finished, cached, or failed)."""
    return cache.progress_store.get(cache.status_progress_key())
