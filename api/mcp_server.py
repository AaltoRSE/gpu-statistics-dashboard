"""MCP server: the dashboard's read-only data as tools for AI agents.

app.py mounts ``build_app()`` at ``/mcp/`` (Streamable HTTP). Each tool
calls the same route function a browser request reaches and validates
the result through that route's response model — the filtering the HTTP
response gets — so an agent and the dashboard never disagree. Tools trim
what an agent rarely needs (raw time series, trend arrays, long lists)
behind ``include_*`` flags and limits, and none takes ``refresh``: an
agent calling in a loop must never bypass the caches that protect sacct,
squeue, NSS and Prometheus.

Every request needs ``Authorization: Bearer $MCP_TOKEN``; with no token
configured the endpoint answers 503 (it fails closed, never open).
"""

import hmac
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

import config
from api import groups, jobs, nodes, partitions, status, users
from api.schemas import (
    GroupMembersResponse,
    GroupsResponse,
    JobDetailResponse,
    JobsResponse,
    NodeDetailResponse,
    NodesResponse,
    PartitionQueueResponse,
    PartitionsResponse,
    StatusHistoryResponse,
    StatusLiveResponse,
    UsersResponse,
)
from deps import DirectoryError
from prom import PrometheusError
from slurm import SlurmError

INSTRUCTIONS = """\
Read-only data from the Triton cluster's GPU efficiency dashboard (Aalto
University): Slurm accounting and state (sacct, scontrol, squeue) and the
Prometheus GPU exporter (per-GPU utilization and VRAM).

- null means a source could not be read, never 0. `*_available: false`,
  `coverage` and `failed_batches` disclose partial data; say so instead of
  reporting a gap as zero.
- `since_hours` (default 24, max 720) looks back from now. Results are
  cached for 30-300 s, so repeating a call is cheap and returns the same
  data within that time.
- cluster_status and cluster_history cover every partition and split jobs
  into CPU jobs (no GPU) and GPU jobs (at least one GPU). Their queue wait
  is Eligible -> Start; partition_queue's completed-job waits are
  Submit -> Start.
- A GPU group (the `partition` of list_jobs, partition_utilization and
  partition_queue) is a Slurm partition, except MIG GPUs, which form their
  own group named by MIG profile (e.g. h200_3g.71gb).
- Utilization is the time-weighted mean of per-GPU samples, in percent.
  Effective GPU-hours = allocated GPU-hours x mean utilization.
- list_groups rolls users up into professor research groups (or
  departments); group_members drills into one row by its group_id.
"""

mcp = MCPServer("triton-gpu-stats", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

SinceHours = Annotated[float, Field(
    gt=0, le=720, description="Look-back window in hours, ending now.")]
RunningOnly = Annotated[bool, Field(
    description="Keep only jobs with a live GPU series right now.")]
Level = Annotated[Literal["group", "department"], Field(
    description="Roll users up by professor group or by department.")]


def _limit(description):
    return Annotated[int, Field(ge=1, le=1000, description=description)]


def _call(route, model, **kwargs):
    """Run one route function as its HTTP request would.

    Every argument is passed explicitly: the routes' defaults are
    FastAPI ``Query(...)`` objects, not values. The result goes through
    the route's response model — dropping internal keys such as a job
    view's ``_util_sum`` or a group row's ``members`` — and the source
    failures app.py maps to a 502 come back as tool errors the model
    can read, under the same error codes.
    """
    try:
        result = route(**kwargs)
    except PrometheusError as exc:
        raise ToolError("prometheus_unreachable: %s" % exc) from exc
    except SlurmError as exc:
        raise ToolError("slurm_unreachable: %s" % exc) from exc
    except DirectoryError as exc:
        raise ToolError("directory_unreachable: %s" % exc) from exc
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    return model.model_validate(result).model_dump(mode="json")


def _summarize_series(series):
    """Per-series statistics in place of the raw time series: each
    utilization and VRAM series' labels (job, GPU, node) with its mean,
    max and last value and sample count — what an agent reads, at a
    fraction of the size."""
    def stats(item):
        values = [v for _, v in item["values"]]
        if not values:
            return {"metric": item["metric"], "samples": 0,
                    "mean": None, "max": None, "last": None}
        return {"metric": item["metric"], "samples": len(values),
                "mean": round(sum(values) / len(values), 2),
                "max": round(max(values), 2),
                "last": round(values[-1], 2)}
    return {kind: [stats(s) for s in series[kind]]
            for kind in ("utilization", "vram")}


@mcp.tool(annotations=READ_ONLY)
def cluster_status() -> dict[str, Any]:
    """The whole cluster right now, every partition: core and GPU
    occupancy (allocated / idle / unavailable), node states, running and
    pending jobs split CPU vs GPU (pending: eligible vs blocked, how long
    the eligible ones have waited), pending reasons, and one row per
    partition. A source that failed has `*_available: false` and null
    figures."""
    return _call(status.api_status_live, StatusLiveResponse, refresh=False)


@mcp.tool(annotations=READ_ONLY)
def cluster_history(
    include_hourly: Annotated[bool, Field(
        description="Also return the hourly series (large).")] = False,
) -> dict[str, Any]:
    """The past 24 hours and past 7 days, every partition, split CPU vs
    GPU: jobs submitted, started, completed, failed, cancelled and other,
    final states, and queue wait (mean / P50 / P90, Eligible -> Start),
    in `summary[day|week][cpu|gpu|all]`. `available: false` means sacct
    could not be read at all; `coverage` discloses partial reads. A cold
    call can take a while (one sacct query per day)."""
    out = _call(status.api_status_history, StatusHistoryResponse,
                refresh=False)
    if not include_hourly:
        out.pop("series", None)
    return out


@mcp.tool(annotations=READ_ONLY)
def list_jobs(
    since_hours: SinceHours = 24,
    user: Annotated[str, Field(
        description="Only this Slurm user (case-insensitive).")] = "",
    partition: Annotated[str, Field(
        description="Only this GPU group (see list_jobs' partitions "
                    "or partition_utilization).")] = "",
    search: Annotated[str, Field(
        description="Job id or job-name substring; searches only the top "
                    "`limit` jobs.")] = "",
    limit: _limit("How many jobs to return, top by effective "
                      "GPU-hours.") = 20,
    running_only: RunningOnly = False,
) -> dict[str, Any]:
    """GPU jobs seen in the window, top `limit` by effective GPU-hours
    (every live job when running_only). Each row: job id, user, GPU
    group, nodes, mean and max utilization, effective and allocated
    GPU-hours, mean VRAM %, and sacct name, state, start and end.
    `total_candidates` counts the matches before the limit."""
    return _call(jobs.api_jobs, JobsResponse, since_hours=since_hours,
                 partition=partition, user=user, search=search, limit=limit,
                 running_only=running_only, refresh=False)


@mcp.tool(annotations=READ_ONLY)
def job_detail(
    jobid: Annotated[str, Field(
        min_length=1, description="Slurm job id, e.g. 1234567.")],
    since_hours: SinceHours = 24,
    include_series: Annotated[bool, Field(
        description="Return the raw per-GPU time series instead of the "
                    "per-GPU summary (large).")] = False,
) -> dict[str, Any]:
    """One job: sacct/scontrol metadata (name, user, state, start, end,
    GPU allocation), mean utilization, effective and allocated GPU-hours
    over the window, and per GPU the mean / max / last utilization and
    VRAM % (`series_summary`)."""
    out = _call(jobs.api_job_detail, JobDetailResponse, jobid=jobid,
                since_hours=since_hours)
    if not include_series:
        out["series_summary"] = _summarize_series(out.pop("series"))
    return out


@mcp.tool(annotations=READ_ONLY)
def list_users(
    since_hours: SinceHours = 24,
    limit: _limit("How many users to return, top by "
                      "utilization-weighted GPU-hours.") = 50,
) -> dict[str, Any]:
    """Per-user GPU activity over the window: jobs, running jobs, mean
    utilization, utilization-weighted GPU-hours, mean VRAM % and GPU
    types, top `limit` by GPU-hours. `count` is the number of users
    before the limit. Use list_jobs(user=...) for one user's jobs."""
    out = _call(users.api_users, UsersResponse, since_hours=since_hours)
    out["users"] = out["users"][:limit]
    return out


@mcp.tool(annotations=READ_ONLY)
def list_groups(
    since_hours: SinceHours = 24,
    running_only: RunningOnly = False,
    level: Level = "group",
    school: Annotated[str, Field(
        description="Only this school's rows, by short name (SCI, ELEC, "
                    "...; see `schools`).")] = "",
    limit: _limit("How many rows to return, top by "
                      "utilization-weighted GPU-hours.") = 50,
) -> dict[str, Any]:
    """GPU efficiency per professor research group (or department) over
    the window: users, jobs, running jobs, sample-weighted mean
    utilization, utilization-weighted and observed GPU-hours, low-
    efficiency jobs (<30%) and top users per row, with the leader,
    department and school. The Unaffiliated row (users in no group,
    including ones the directory does not know) is always present;
    `coverage` counts how users were classified. `count` is the number
    of rows before the limit."""
    out = _call(groups.api_groups, GroupsResponse, since_hours=since_hours,
                running_only=running_only, level=level)
    rows = out["groups"]
    if school:
        rows = [r for r in rows
                if (r["school_code"] or "").casefold() == school.casefold()]
    out["count"] = len(rows)
    out["groups"] = rows[:limit]
    return out


@mcp.tool(annotations=READ_ONLY)
def group_members(
    group_id: Annotated[str, Field(
        min_length=1, description="A row's group_id from list_groups.")],
    since_hours: SinceHours = 24,
    running_only: RunningOnly = False,
    level: Level = "group",
) -> dict[str, Any]:
    """Every member of one list_groups row, with their own activity
    (jobs, mean utilization, GPU-hours, VRAM, GPU types) and how they
    belong to it (leader, paid, staff, everyone or external). Pass the
    same since_hours, running_only and level the row came from."""
    return _call(groups.api_group_users, GroupMembersResponse,
                 group_id=group_id, since_hours=since_hours,
                 running_only=running_only, level=level)


@mcp.tool(annotations=READ_ONLY)
def partition_utilization(
    since_hours: SinceHours = 24,
    running_only: RunningOnly = False,
    include_trend: Annotated[bool, Field(
        description="Also return each group's utilization time series "
                    "(large).")] = False,
) -> dict[str, Any]:
    """Per GPU group over the window: time-weighted mean and max
    utilization, mean occupancy (allocated share of capacity), and live
    allocated / total GPUs (idle capacity included)."""
    out = _call(partitions.api_partitions, PartitionsResponse,
                since_hours=since_hours, running_only=running_only)
    if not include_trend:
        out.pop("trend", None)
    return out


@mcp.tool(annotations=READ_ONLY)
def partition_queue(
    since_hours: SinceHours = 24,
    running_only: RunningOnly = False,
    limit: _limit("How many waiting jobs to list.") = 50,
) -> dict[str, Any]:
    """The GPU queue per GPU group: pending jobs and GPUs requested now
    (a job eligible for several groups appears in each, so rows must not
    be summed; `totals` counts each job once) and completed-job wait
    statistics over the window (Submit -> Start). `queue_available` or
    `wait_history_available` false means that half could not be read.
    `waiting_jobs` lists up to `limit` pending GPU jobs;
    `waiting_jobs_total` counts them all."""
    out = _call(partitions.api_partition_queue, PartitionQueueResponse,
                since_hours=since_hours, running_only=running_only)
    out["waiting_jobs_total"] = len(out["waiting_jobs"])
    out["waiting_jobs"] = out["waiting_jobs"][:limit]
    return out


@mcp.tool(annotations=READ_ONLY)
def list_nodes(
    gpu_only: Annotated[bool, Field(
        description="Only nodes with GPUs.")] = True,
) -> dict[str, Any]:
    """Every node's state (with drain/down reason), partitions, CPUs and
    GPUs allocated / total, GPU type, live GPU utilization and VRAM %,
    and up to 10 active jobs by utilization."""
    return _call(nodes.api_nodes, NodesResponse, gpu_only=gpu_only,
                 refresh=False)


@mcp.tool(annotations=READ_ONLY)
def node_detail(
    name: Annotated[str, Field(
        min_length=1, description="Node name, e.g. gpu42.")],
    view: Annotated[Literal["job_start", "1", "6", "24"], Field(
        description="job_start: since the earliest active job on the "
                    "node started; otherwise the past 1, 6 or 24 "
                    "hours.")] = "job_start",
    include_series: Annotated[bool, Field(
        description="Return the raw per-GPU time series instead of the "
                    "per-GPU summary (large).")] = False,
) -> dict[str, Any]:
    """One node's per-GPU, per-job utilization and VRAM % over the view:
    mean / max / last value per series (`series_summary`)."""
    out = _call(nodes.api_node_detail, NodeDetailResponse, name=name,
                view=view)
    if not include_series:
        out["series_summary"] = _summarize_series(out.pop("series"))
    return out


class BearerAuth:
    """ASGI wrapper: a request reaches the MCP app only with
    ``Authorization: Bearer <MCP_TOKEN>``. No token configured is a 503
    (the endpoint stays off rather than open); a missing or wrong token
    is a 401. The comparison is constant-time."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = config.mcp_token()
        if token is None:
            response = JSONResponse(
                {"error": "mcp_disabled",
                 "detail": "set MCP_TOKEN to enable the MCP endpoint"},
                status_code=503)
        else:
            scheme, _, given = Headers(scope=scope).get(
                "authorization", "").partition(" ")
            if scheme.lower() == "bearer" and hmac.compare_digest(
                    given.strip().encode(), token.encode()):
                await self.app(scope, receive, send)
                return
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)


def build_app():
    """The ASGI app app.py mounts at /mcp: the Streamable HTTP endpoint
    at the mount root, behind BearerAuth.

    Stateless with plain JSON responses, so no session outlives a
    request (a restart drops nothing) and no client needs SSE. The SDK's
    DNS-rebinding Host check is off because the bearer token is
    mandatory: a rebound browser page cannot supply it, and the check
    would otherwise answer every request addressed to the cluster
    hostname with a 421. Call once: the session manager the host
    lifespan enters (``mcp.session_manager``) is created here.
    """
    return BearerAuth(mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False),
    ))
