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
    fairshare: Optional[float] = Field(
        default=None,
        description="Slurm's Fair Tree fairshare factor (0-1, higher "
                    "starts sooner) of the user's primary association "
                    "(fairshare_account); null when sshare could not be "
                    "read or does not list the user.")
    fairshare_account: Optional[str] = Field(
        default=None,
        description="The account of the association fairshare comes "
                    "from: the one with the most decayed usage, else the "
                    "user's default account.")


class UsersResponse(BaseModel):
    window: Window
    count: int
    fairshare_available: bool = Field(
        description="False when sshare could not be read: every row's "
                    "fairshare is then null, not 0.")
    users: List[UserRow]


# ---- /api/users/{user}/priority ---------------------------------------

class FairshareAssociation(BaseModel):
    account: str
    raw_shares: Optional[int] = None
    norm_shares: Optional[float] = Field(
        default=None,
        description="The association's share of its parent account's "
                    "shares.")
    raw_usage: Optional[int] = Field(
        default=None,
        description="Decayed usage (TRES-billing seconds; 14-day "
                    "half-life on Triton).")
    effective_usage: Optional[float] = Field(
        default=None,
        description="The association's share of its parent account's "
                    "usage.")
    fairshare: Optional[float] = Field(
        default=None,
        description="Fair Tree fairshare factor, 0-1, higher starts "
                    "sooner.")
    primary: bool = Field(
        description="The association the Users table's fairshare "
                    "column reports.")


class PendingPriority(BaseModel):
    jobid: str
    partition: str
    account: str
    qos: str
    priority: Optional[int] = Field(
        default=None,
        description="The job's priority in this partition: the weighted "
                    "factors below summed, minus nice.")
    age: Optional[int] = None
    fairshare: Optional[int] = None
    jobsize: Optional[int] = None
    partition_factor: Optional[int] = None
    qos_factor: Optional[int] = None
    nice: Optional[int] = None
    rank: int = Field(
        description="1 + the number of pending jobs in this partition "
                    "with a higher priority (ties share a rank). An "
                    "order by priority, not a start-time forecast: "
                    "backfill can start lower-ranked jobs first.")
    queue_length: int = Field(
        description="Pending jobs in this partition, this one included.")


class UserPriorityResponse(BaseModel):
    user: str
    default_account: Optional[str] = Field(
        default=None,
        description="sacctmgr's default account; null when unknown or "
                    "sacctmgr could not be read.")
    fairshare_available: bool
    associations: Optional[List[FairshareAssociation]] = Field(
        default=None,
        description="Every (account, user) association; null when sshare "
                    "could not be read, empty for a user it does not "
                    "list.")
    pending_available: bool
    pending: Optional[List[PendingPriority]] = Field(
        default=None,
        description="One row per pending job and partition it may run "
                    "in, by partition then rank; null when sprio could "
                    "not be read.")


# ---- /api/groups, /api/groups/{group_id}/users -------------------------

class School(BaseModel):
    code: str = Field(description="Department-code prefix the school's "
                      "departments start with (T1-T6, A8, E7, ...).")
    short: str = Field(description="Short name used as the school filter "
                       "value and on rows (SCI, ELEC, ...; Other for "
                       "legacy/university prefixes).")
    full: Optional[str] = Field(
        default=None,
        description="Full school name from prof_groups.conf; null for the "
        "synthetic Other a department code that matches no prefix gets.")


class GroupTopUser(BaseModel):
    user: str
    util_gpu_hours: float


class GroupCoverage(BaseModel):
    users: int = Field(description="Distinct job owners the roll-up "
                       "attempted to classify.")
    in_prof_group: int = Field(description="Users classified into a "
                               "professor research group.")
    dept_only: int = Field(description="Users with an osasto department "
                           "but no professor group; their row always "
                           "renders.")
    unaffiliated: int = Field(description="Known users with no "
                              "professor group and no osasto-t* group; "
                              "their row always renders.")
    unresolved: int = Field(description="Users the directory does not "
                            "know; their activity rolls up under the "
                            "always-present Unaffiliated row.")
    failed: int = Field(description="Users whose NSS lookup errored "
                        "(partial directory outage). Their activity is in "
                        "no row — disclosed here, not folded into "
                        "unaffiliated.")


class GroupRow(BaseModel):
    group_id: str = Field(description="Roll-up row identity: the group "
          "leader's username, unit:<CODE> for a shared unit (several "
          "professors share the unit, so the row has no single leader), "
          "dept:TNNN ('<Department>, no professor group' at group "
          "level), or the always-present unaffiliated row — unresolved "
          "users (the directory does not know them) roll up under it "
          "too. Use it for the drill-down path.")
    group_name: str
    leader: Optional[str] = Field(
        default=None,
        description="The professor group's leader username; null for "
        "shared-unit, department and special rows.")
    leader_name: Optional[str] = Field(
        default=None,
        description="The leader's display name from prof_groups.conf; "
        "null for shared-unit, department and special rows (the unit "
        "name itself is the row's group_name).")
    unit_codes: List[str] = Field(
        default_factory=list,
        description="The AD unit codes the group reads its NSS member "
        "groups from (T40106, ...); empty for department and special "
        "rows.")
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    school_code: Optional[str] = Field(
        default=None,
        description="School short name (SCI, ELEC, ...), Other when the "
        "department code matches no school prefix, null for the "
        "Unaffiliated row.")
    school_name: Optional[str] = None
    users: int
    jobs: int
    running_jobs: int
    mean_util: float = Field(
        description="Sample-weighted mean utilization across ALL member "
        "jobs' GPU series (sum of per-job sample sums / sum of sample "
        "counts) — not the mean of per-user means.")
    util_gpu_hours: float = Field(
        description="Utilization-weighted GPU-hours, summed over members' "
        "Users-tab GPU-hours; a group's figure equals the sum of its "
        "members' values there.")
    gpu_hours: float = Field(
        description="Observed GPU-hours: the window GPU time the members' "
        "GPU series covered (the denominator of the utilization "
        "weighting) — the only allocation figure the Prometheus-only "
        "pipeline has, since the Groups pipeline runs no sacct fetch.")
    vram_avg: Optional[float] = None
    low_eff_jobs: int = Field(
        description="Member jobs whose mean utilization is under 30%.")
    top_users: List[GroupTopUser]


class GroupsResponse(BaseModel):
    window: Window
    level: str = Field(description="The roll-up level this response used "
                       "(group or department).")
    schools: List[School]
    coverage: GroupCoverage
    count: int
    groups: List[GroupRow]


class GroupMember(BaseModel):
    user: str
    jobs: int
    running_jobs: int
    mean_util: float
    util_gpu_hours: float
    vram_avg: Optional[float] = None
    gpu_types: List[str]
    group: Optional[str] = Field(
        default=None,
        description="The user's primary professor group (the leader's "
        "username); null for department-only and Unaffiliated users — "
        "unresolved users (the directory does not know them) among "
        "them.")
    membership: Optional[str] = Field(
        default=None,
        description="How the user belongs to their primary group: "
        "leader, paid (the unit's laitos group), external (the unit's "
        "auto-ext group — a visitor from outside the university), staff "
        "or everyone.")
    dept_code: Optional[str] = None
    school_code: Optional[str] = None
    own_dept: Optional[str] = Field(
        default=None,
        description="The user's own osasto department — for a group "
        "member this can differ from the row's department, which is the "
        "professor's.")
    extra_groups: List[str] = Field(
        description="The user's other professor groups when several "
        "claim them (the roll-up uses the strongest membership).")
    status: str


class GroupMembersResponse(BaseModel):
    group_id: str
    group_name: str
    level: str
    window: Window
    count: int
    users: List[GroupMember]


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


# ---- /api/status/live, /api/status/history ----------------------------

class Capacity(BaseModel):
    """One resource's allocation right now, over every node."""
    alloc: int
    idle: int = Field(description="Unallocated capacity on nodes that can "
                                  "take new work.")
    unavailable: int = Field(description="Unallocated capacity on down, "
                                         "draining or otherwise "
                                         "unavailable nodes.")
    total: int
    occupancy_pct: Optional[float] = Field(
        default=None,
        description="alloc / total x 100 (the figure sinfo %C reports); "
                    "null for a resource the cluster has none of.")


class NodeStateCount(BaseModel):
    state: str
    cpu_nodes: int
    gpu_nodes: int


class NodeSummary(BaseModel):
    total: int
    unavailable: int
    by_state: List[NodeStateCount]


class RunningClass(BaseModel):
    jobs: int
    cpus: int = Field(description="Cores these running jobs hold.")
    gpus: int = Field(description="GPUs these running jobs hold.")


class PendingClass(BaseModel):
    jobs: int = Field(description="Pending job records; a pending array "
                                  "is one record.")
    tasks: int = Field(description="Array tasks those records stand for "
                                   "(a plain job counts 1).")
    eligible_jobs: int = Field(description="Records the scheduler may "
                                           "start now (queued for "
                                           "resources or priority).")
    blocked_jobs: int = Field(description="Records a dependency or hold "
                                          "still keeps back.")
    eligible_age_avg_s: Optional[int] = Field(
        default=None, description="Mean time since the eligible records "
                                  "became eligible; null with none.")
    eligible_age_max_s: Optional[int] = None


class PendingReason(BaseModel):
    reason: str
    cpu_jobs: int
    gpu_jobs: int


class StatusPartition(BaseModel):
    name: str
    kind: str = Field(description='"gpu" when the partition has GPU '
                                  'nodes, else "cpu".')
    nodes: int
    cpus_alloc: int
    cpus_total: int
    gpus_alloc: int
    gpus_total: int
    running_jobs: Optional[int] = Field(
        default=None, description="Null when the scontrol job snapshot "
                                  "is unavailable.")
    pending_jobs: Optional[int] = Field(
        default=None,
        description="Pending records listing this partition; a job that "
                    "lists several partitions counts in each, so this "
                    "column must not be summed. Null when squeue is "
                    "unavailable.")


class StatusLiveResponse(BaseModel):
    time: int
    nodes_available: bool
    jobs_available: bool
    queue_available: bool = Field(
        description="False when squeue failed: pending figures are then "
                    "null, never an empty queue.")
    cpu: Optional[Capacity] = None
    gpu: Optional[Capacity] = None
    nodes: Optional[NodeSummary] = None
    running: Optional[Dict[str, RunningClass]] = Field(
        default=None, description='Running jobs keyed "cpu" / "gpu".')
    pending: Optional[Dict[str, PendingClass]] = Field(
        default=None, description='Pending jobs keyed "cpu" / "gpu".')
    pending_reasons: Optional[List[PendingReason]] = None
    partitions: Optional[List[StatusPartition]] = None


class StatusSummary(BaseModel):
    """One window x job class of the history."""
    submitted: int = Field(description="Jobs submitted in the window; "
                                       "array tasks count individually.")
    started: int
    completed: int
    failed: int = Field(description="FAILED, OUT_OF_MEMORY, TIMEOUT, "
                                    "NODE_FAIL, BOOT_FAIL and DEADLINE.")
    cancelled: int
    other: int = Field(description="Any other final state (PREEMPTED, "
                                   "REVOKED, ...).")
    ended: int
    states: Dict[str, int] = Field(description="Final-state counts behind "
                                               "the outcome groups.")
    wait_p50_s: Optional[int] = None
    wait_p90_s: Optional[int] = None
    wait_avg_s: Optional[int] = Field(
        default=None, description="Mean Eligible -> Start queue wait of "
                                  "the jobs started in the window.")
    wait_samples: int


class StatusWindows(BaseModel):
    day: Window
    week: Window


class StatusClassSeries(BaseModel):
    submitted: List[Optional[int]]
    started: List[Optional[int]]
    completed: List[Optional[int]]
    failed: List[Optional[int]]
    cancelled: List[Optional[int]]
    other: List[Optional[int]]
    wait_avg_s: List[Optional[int]]
    wait_p50_s: List[Optional[int]]
    cpus_alloc: List[Optional[float]] = Field(
        description="Average cores held per hour.")
    gpus_alloc: List[Optional[float]] = Field(
        description="Average GPUs held per hour.")


class StatusSeries(BaseModel):
    t: List[int] = Field(description="Hour-start epochs; the last hour "
                                     "is the current, partial one.")
    cpu: StatusClassSeries
    gpu: StatusClassSeries


class StatusCoverage(BaseModel):
    chunks: int
    failed_chunks: int
    complete: bool
    records: int
    stale_running_rows: int = Field(
        description="sacct rows still marked running for jobs the "
                    "controller no longer runs; excluded from the "
                    "resources-held series.")
    unverified_running_rows: int = Field(
        description="Running rows counted without the live job set to "
                    "confirm them (scontrol unavailable).")
    running_verified: bool
    blocked_pending_counted: bool = Field(
        description="False when squeue (or the live-edge chunk) was "
                    "unavailable: submissions still blocked by a "
                    "dependency or hold — which sacct cannot see — are "
                    "then missing from 'submitted'.")
    blocked_pending_jobs: int = Field(
        description="Pending jobs, never yet eligible, added to "
                    "'submitted' from the squeue snapshot.")


class StatusCapacity(BaseModel):
    cpus: int
    gpus: int


class StatusHistoryResponse(BaseModel):
    available: bool = Field(
        description="False when sacct could not be read at all; the "
                    "other fields are then null — never an idle week.")
    window: Optional[Window] = None
    windows: Optional[StatusWindows] = None
    summary: Optional[Dict[str, Dict[str, StatusSummary]]] = Field(
        default=None,
        description='summary["day"|"week"]["cpu"|"gpu"|"all"].')
    series: Optional[StatusSeries] = None
    coverage: Optional[StatusCoverage] = None
    capacity: Optional[StatusCapacity] = Field(
        default=None,
        description="Current total cores and GPUs (scontrol), the "
                    "reference lines of the resources-held charts; "
                    "null when scontrol is unavailable.")
