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
                    "job's GPU type (short scontrol GRES type; MIG GPUs "
                    "group by their own profile so a MIG node's capacity "
                    "never counts against the whole-GPU pool), resolved "
                    "from the job's observed nodes — never the Slurm "
                    "partition name.")
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
    name: str = Field(description="Canonical GPU type (short scontrol "
                      "GRES type; MIG profiles keep their profile name) "
                      "— partitions over the same hardware share one "
                      "row.")
    mean_util: Optional[float] = Field(
        description="Time-weighted mean utilization over the window; null "
                    "when this configured GPU type has no utilization "
                    "samples.")
    max_util: Optional[float] = Field(
        description="Peak utilization over the window; null when this "
                    "configured GPU type has no utilization samples.")
    job_count: int
    gpus_alloc: int = Field(description="Live allocated GPU count.")
    gpus_total: int = Field(description="Total scontrol GPU capacity of "
                            "the group's nodes, idle included.")
    mean_occupancy: Optional[float] = Field(
        default=None,
        description="Window-average share of gpus_total with an active "
                    "job (%); null when utilization-count samples or "
                    "capacity are unavailable.")


class QueueGroup(BaseModel):
    """One GPU type's pending demand and completed-wait statistics.

    Per-type figures overlap by design: a flexible (multi-type) pending
    job appears in every eligible row, so columns must not be summed
    across rows. The cluster-wide unique figures live in
    ``PartitionQueueResponse.totals``.
    """
    exclusive_jobs: Optional[int] = Field(
        default=0,
        description="Pending (PD) jobs eligible only for this GPU "
                    "type; null when squeue is unavailable.")
    flexible_jobs: Optional[int] = Field(
        default=0,
        description="Pending jobs eligible for this type and at least "
                    "one other; also counted in those rows; null when "
                    "squeue is unavailable.")
    eligible_jobs: Optional[int] = Field(
        default=0,
        description="Jobs that could run on this GPU type (exclusive + "
                    "flexible); non-additive across rows; null when "
                    "squeue is unavailable.")
    exclusive_gpus: Optional[int] = Field(
        default=0,
        description="GPUs requested by this type's exclusive pending "
                    "jobs; null when squeue is unavailable.")
    flexible_gpus: Optional[int] = Field(
        default=0,
        description="GPUs requested by this type's flexible pending "
                    "jobs; also counted in their other rows; null when "
                    "squeue is unavailable.")
    eligible_gpus: Optional[int] = Field(
        default=0,
        description="GPUs requested by all pending jobs eligible for "
                    "this type (exclusive + flexible); non-additive "
                    "across rows; null when squeue is unavailable.")
    wait_p50_s: Optional[int] = Field(
        default=None,
        description="Median completed-job Submit → Start wait in "
                    "seconds over this type's valid started jobs in "
                    "the window; null with no samples.")
    wait_p90_s: Optional[int] = Field(
        default=None,
        description="Nearest-rank P90 completed-job wait in seconds; "
                    "null with no samples.")
    wait_avg_s: Optional[int] = Field(
        default=None,
        description="Mean completed-job wait in seconds; null with no "
                    "samples.")
    wait_samples: Optional[int] = Field(
        default=0,
        description="Number of valid completed-job waits behind the "
                    "percentile/average figures; null when the sacct "
                    "enrichment failed (distinct from a genuine 0).")
    wait_per_gpu_hour_weighted: Optional[float] = Field(
        default=None,
        description="Completed-job queue-wait hours per allocated "
                    "GPU-hour, GPU-hour weighted: sum(wait_hours) / "
                    "sum(elapsed_hours × GPUs); lower is better. "
                    "Weighted by job size, so short jobs cannot "
                    "dominate the figure. Null when no completed job "
                    "has valid positive elapsed time and GPU "
                    "allocation, or when wait history is unavailable.")


class PendingJob(BaseModel):
    jobid: str
    user: str
    partition: str = Field(
        description="The job's raw squeue partition list (may be several, "
                    "comma-separated) — scheduler metadata only; the "
                    "Partitions tab groups and filters by GPU type, not "
                    "by this string.")
    state: str
    submit: str = Field(description="Raw squeue submit time string.")
    start: str = Field(
        description="Slurm's estimated start time; empty when none.")
    reason: str
    nodes: int
    gpus: int = Field(description="Per-node GPU request (0 for CPU jobs).")
    gpu_type: str
    groups: List[str] = Field(
        description="Canonical GPU types this pending job counts toward "
                    "(its typed %b request, else the GPU-type union of "
                    "its requested partitions); client-side type "
                    "filtering keys on this, not on the raw partition "
                    "string.")
    wait_s: Optional[int] = Field(
        default=None,
        description="Seconds since submit at response time; null when the "
                    "submit time does not parse.")
    gpu_total: Optional[int] = Field(
        default=None,
        description="The job's requested GPU count (0 for a "
                    "constraints-only GPU-partition row).")


class PartitionsResponse(BaseModel):
    """Fast Prometheus-backed utilization payload.

    Queue and wait-history fields deliberately live on
    ``PartitionQueueResponse`` (``/api/partitions/queue``): squeue and
    sacct are slower than Prometheus, so the Partitions tab fetches the
    two endpoints concurrently and renders whichever arrives first
    instead of blocking every chart on the slowest source.
    """
    window: Window
    step: int
    partitions: List[PartitionRow]
    trend: Dict[str, List[Tuple[float, float]]] = Field(
        description="Per-group utilization trend series, keyed by group "
                    "name.")


class QueueTotals(BaseModel):
    """Unique cluster-wide pending demand; each physical job once."""
    unique_pending_jobs: Optional[int] = Field(
        default=None,
        description="GPU-eligible pending jobs, each counted once "
                    "regardless of how many type rows it is eligible "
                    "for; null when squeue is unavailable.")
    unique_gpus_requested: Optional[int] = Field(
        default=None,
        description="GPUs requested by those pending jobs; null when "
                    "squeue is unavailable.")


class WaitHistoryCoverage(BaseModel):
    """Completeness and exclusions behind completed-job wait metrics."""
    records_examined: int = 0
    valid_samples: Dict[str, int] = Field(default_factory=dict)
    excluded: Dict[str, int] = Field(default_factory=dict)
    failed_batches: int = 0
    complete: bool = True


class PartitionQueueResponse(BaseModel):
    queue: Dict[str, QueueGroup] = Field(
        default_factory=dict,
        description="Pending-job demand and completed-wait statistics "
                    "per GPU-type group; includes zero-pending types so "
                    "every visible group renders. Per-type columns "
                    "overlap (flexible jobs) and must not be summed.")
    totals: QueueTotals = Field(
        default_factory=QueueTotals,
        description="Cluster-wide unique pending figures: every "
                    "GPU-eligible physical job once. Null fields when "
                    "squeue is unavailable.")
    queue_available: bool = Field(
        default=True,
        description="False when the squeue snapshot failed (squeue "
                    "missing or erroring) — ``queue``'s pending figures "
                    "are then unavailable and waiting_jobs is empty; must "
                    "not be read as an empty queue.")
    waiting_jobs: List[PendingJob] = Field(
        default_factory=list,
        description="GPU-eligible jobs still waiting, one record per "
                    "physical pending job (each appears once even when "
                    "it is eligible for several GPU types; CPU-only "
                    "pending jobs are not listed). Empty when squeue is "
                    "unavailable.")
    wait_history_available: bool = Field(
        default=True,
        description="False when the sacct enrichment behind the wait "
                    "statistics failed — queue current-pending figures "
                    "stay valid; must not be read as 'no jobs started "
                    "in the window'.")
    wait_history_coverage: Optional[WaitHistoryCoverage] = Field(
        default=None,
        description="Accounting coverage behind completed-job wait metrics; "
                    "null when sacct could not be queried.")


class VramRecord(BaseModel):
    jobid: str
    user: str
    partition: str = Field(description="The job's canonical GPU type "
                          "(same key as the Partitions tab's groups).")
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
        description="Returned candidate job count; every VRAM-bearing job "
                    "in the window (and partition filter) is returned, so "
                    "this equals len(jobs).")
    enriched_frac: float = Field(
        default=0.0,
        description="Fraction of the returned records whose allocated "
                    "GPU-hours sacct resolved; below 1.0 discloses partial "
                    "accounting coverage (some records' gpu_hours stay "
                    "null) rather than a silent gap.")
    failed_batches: int = Field(
        default=0,
        description="Number of 100-ID sacct enrichment batches that failed "
                    "after retrying; those records' gpu_hours stay null. "
                    "Zero means the enrichment is complete.")
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
    gpu_group: str = Field(
        description="Canonical GPU-type group for this node's GPUs — "
                    "its scalar gpu_type (the MIG profile for an all-MIG "
                    "node); see Job.gpu_group.")
    cpus_alloc: int
    free_mem: int
    real_mem: int
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
