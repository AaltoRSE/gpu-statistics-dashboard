"""Response models for every JSON-returning route.

Before this, every endpoint returned a bare dict: /docs showed no
response shapes, and the only way to learn what a field means (or
that it exists at all) was reading app.js. These models are the
declared contract; FastAPI validates every response against them and
drops anything not declared here — which is also what replaces
_public_job's manual dict-comprehension filtering of the internal
_util_sum/_util_samples aggregands.

A field that the underlying code sometimes omits entirely (e.g. a job
whose sacct/scontrol lookup found no match never gets a `name` key at
all) is modeled as Optional with a None default: FastAPI then always
includes the key, as null when absent. That is a deliberate, minor
normalization — a predictable "always present, sometimes null" field
is a clearer contract than "sometimes present, sometimes absent" — and
is why the golden fixtures needed a `--update-golden` pass alongside
this change rather than staying byte-identical.
"""

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class Window(BaseModel):
    start: int
    end: int


class SeriesItem(BaseModel):
    metric: Dict[str, str]
    values: List[Tuple[float, float]]


class Series(BaseModel):
    utilization: List[SeriesItem]
    vram: List[SeriesItem]


# ---- /api/jobs, /api/jobs/{jobid} -----------------------------------

class Job(BaseModel):
    jobid: str
    user: str
    partition: str = Field(
        description="Raw Prometheus/sacct partition label. Prefer gpu_group "
                    "for display and filtering — see gpu_group.")
    gpu_type: str
    gpu_group: str = Field(
        description="Canonical grouping used by the Partitions tab: the "
                    "Slurm partition, except MIG GPUs, which group by their "
                    "own profile so a MIG node's capacity never counts "
                    "against the whole-GPU pool.")
    nodes: List[str]
    mean_util: float = Field(
        description="Time-weighted mean GPU utilization over the window "
                    "(%). Also referred to as a job's \"efficiency\" "
                    "elsewhere in this API (efficiency_histogram below) "
                    "and in the UI — that is this same field, not a "
                    "separate one.")
    max_util: float
    gpu_hours_eff: float = Field(
        description="Effective GPU-hours: allocated GPU-hours x mean "
                    "utilization once sacct/scontrol metadata resolves "
                    "(see gpu_hours_alloc); the Prometheus-only estimate "
                    "before that.")
    vram_avg: Optional[float] = None
    name: Optional[str] = None
    state: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    node_list: Optional[str] = None
    account: Optional[str] = None
    gpus: Optional[int] = None
    ncpus: Optional[int] = None
    gpu_hours_alloc: Optional[float] = Field(
        default=None,
        description="Allocated GPU-hours (gpus x elapsed hours) from sacct/"
                    "scontrol. Present only once metadata resolution finds "
                    "a matching record — an unmatched job has no allocation "
                    "source, so this (and gpu_hours_eff's allocation-based "
                    "value) stays absent as null.")


class EfficiencyHistogramBin(BaseModel):
    bucket_start: int = Field(description="Inclusive lower bound of the "
                              "mean-utilization bucket (%).")
    bucket_end: int = Field(description="Exclusive upper bound of the "
                            "mean-utilization bucket (%).")
    gpu_hours: float = Field(
        description="Summed gpu_hours_eff of jobs whose mean_util falls "
                    "in this bucket.")


class JobsResponse(BaseModel):
    window: Window
    count: int
    total_candidates: int = Field(
        description="Jobs matching partition/user/running_only before the "
                    "sacct-enrichment cap ('show top N by GPU-hours') is "
                    "applied. Search runs after that cap, so total_candidates "
                    "> count with an empty search result means the match "
                    "exists but fell outside the top-N by GPU-hours, not "
                    "that it doesn't exist in the window.")
    partitions: List[str]
    jobs: List[Job]
    efficiency_histogram: List[EfficiencyHistogramBin] = Field(
        description="GPU-hours consumed by mean-utilization bucket (10%-"
                    "wide, 0-100), over the same pre-limit candidate set as "
                    "total_candidates. Shows where capacity is wasted "
                    "without the ranked highest/lowest lists this replaced "
                    "going degenerate on a small candidate set.")


class JobDetailResponse(BaseModel):
    jobid: str
    window: Window
    step: int
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="sacct/scontrol metadata for this job, or null if "
                    "neither source has a matching record. Shape varies: "
                    "an array-parent job additionally carries array_jobid/"
                    "array_task_id/allocation_seconds from the merge across "
                    "its physical tasks.")
    series: Series
    mean_util: float = Field(
        description="Time-weighted mean GPU utilization over the window "
                    "(%), across every matched GPU series — same figure as "
                    "Job.mean_util.")
    gpu_hours_eff: float = Field(
        description="Effective GPU-hours: allocated GPU-hours x mean "
                    "utilization once metadata resolves (see "
                    "gpu_hours_alloc); the Prometheus-only estimate before "
                    "that — same figure as Job.gpu_hours_eff.")
    gpu_hours_alloc: Optional[float] = Field(
        default=None,
        description="Allocated GPU-hours (gpus x elapsed hours) from "
                    "metadata; null when metadata didn't resolve.")
    elapsed_s: Optional[int] = Field(
        default=None,
        description="The job's own elapsed running time in seconds, from "
                    "metadata; null when metadata didn't resolve. Not the "
                    "query window's length.")


# ---- /api/users -------------------------------------------------------

class UserRow(BaseModel):
    user: str
    jobs: int
    running_jobs: int
    mean_util: float = Field(
        description="Sample-weighted mean utilization across the user's "
                    "GPU series over the window.")
    util_gpu_hours: float = Field(
        description="Utilization-weighted GPU-hours (mean util x GPU "
                    "time) — not the same figure as a job's "
                    "gpu_hours_eff, which is allocation-based.")
    vram_avg: Optional[float] = None
    gpu_types: List[str]


class UsersResponse(BaseModel):
    window: Window
    count: int
    users: List[UserRow]


# ---- /api/partitions, /api/partitions/vram -----------------------------

class PartitionRow(BaseModel):
    name: str
    mean_util: float = Field(
        description="Time-weighted mean utilization over the window.")
    max_util: float
    job_count: int
    gpus_alloc: int = Field(description="Live allocated GPU count.")
    gpus_total: int = Field(description="Total scontrol GPU capacity of "
                            "the group's nodes, idle included.")
    mean_occupancy: Optional[float] = Field(
        default=None,
        description="Window-average share of gpus_total with an active "
                    "job (%); null when capacity is unknown.")


class PartitionsResponse(BaseModel):
    window: Window
    step: int
    partitions: List[PartitionRow]
    trend: Dict[str, List[Tuple[float, float]]] = Field(
        description="Per-group utilization trend series, keyed by group "
                    "name.")


class VramRecord(BaseModel):
    jobid: str
    user: str
    partition: str = Field(description="The job's canonical GPU group.")
    gpu_type: str
    mean_util: float
    vram_gb: float = Field(
        description="Average per-GPU peak VRAM over the window, in GB.")
    gpu_hours: Optional[float] = Field(
        default=None,
        description="Allocated GPU-hours from sacct; null when sacct "
                    "enrichment found no matching record.")
    gpu_hours_eff: float = Field(
        description="Effective GPU-hours (allocated x mean utilization) "
                    "computed from the Prometheus window, independent of "
                    "sacct.")


class VramResponse(BaseModel):
    window: Window
    step: int
    total: int = Field(
        description="Candidate job count before the sacct-enrichment cap; "
                    "jobs may hold fewer records than this.")
    jobs: List[VramRecord]


# ---- /api/nodes, /api/nodes/{name} -------------------------------------

class ActiveJob(BaseModel):
    jobid: str
    job: str
    user: str
    util: float


class NodeRow(BaseModel):
    name: str
    state: str = Field(description="The node's base state (qualifiers "
                       "stripped) — see state_full for the full value.")
    state_full: str = Field(
        description="scontrol state including every '+'-joined qualifier "
                    "(e.g. IDLE+DRAIN).")
    reason: str = Field(
        description="Drain/down reason text; empty string when none.")
    partitions: str = Field(description="Comma-separated Slurm partitions "
                            "this node belongs to.")
    cpus: int
    gpus: int
    gpu_type: str
    cpus_alloc: int
    free_mem: int
    real_mem: int
    gpu_group: str = Field(
        description="Canonical group for this node's GPUs — see Job.gpu_group.")
    current_util: Optional[float] = Field(
        default=None, description="Instant GPU utilization (%); null when idle.")
    current_vram: Optional[float] = None
    gpus_alloc: int
    active_jobs: List[ActiveJob] = Field(
        description="Up to 10 active jobs on this node, by utilization "
                    "descending.")


class NodesResponse(BaseModel):
    time: int
    count: int
    nodes: List[NodeRow]


class NodeDetailResponse(BaseModel):
    node: str
    view: str
    window: Window
    step: int
    series: Series


# ---- /api/health --------------------------------------------------------

class HealthResponse(BaseModel):
    ok: bool
    prometheus: str
    time: str
