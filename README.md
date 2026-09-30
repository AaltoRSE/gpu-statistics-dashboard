# Triton GPU Efficiency Dashboard

Live admin dashboard for the Triton cluster's **status** (occupancy, queue
and job throughput, cluster-wide) and for tracking **jobs**, **partitions**,
and **nodes** GPU efficiency. A FastAPI backend collects data **on demand**
(queried live while the admin works with the dashboard — no periodic
collection) from:

- **`sacct`** — job metadata (name, user, state, start/end, GPU allocation)
  and the Status tab's cluster-wide job history
- **`scontrol`** — node state, CPU and GPU capacity, running jobs
- **`squeue`** — the pending-job queue (`-t PD`) behind the Partitions tab's
  queue status and the Status tab's pending jobs
- **Prometheus** (`stats.triton.aalto.fi`) — `slurm_job_*` exporter metrics:
  per-GPU utilization and VRAM, live and historical

The frontend is a plain-JS single page (Plotly.js via CDN) with five
interactive tabs; `/` opens the Status tab.

A **light/dark theme** toggle sits in the header (top right). Dark is the
default; the light preference is remembered in `localStorage` and the
OS-level preference applies when no choice has been saved.

## Features

### Status tab
The cluster at a glance — every partition, CPU and GPU alike — with every
job figure split into **CPU jobs** (no GPU requested or held) and **GPU
jobs** (at least one GPU). Two panels load and fail independently.

- **Right now** (scontrol + squeue, 30 s snapshots): core and GPU
  occupancy (allocated / total over every node, the `sinfo %C` figure;
  unallocated capacity on down or draining nodes is *unavailable*),
  running jobs, pending jobs (a pending array counts once, its array
  tasks alongside; *eligible* jobs wait for resources or priority,
  *blocked* ones on a dependency or hold), how long the eligible ones
  have waited so far, and unavailable nodes. A stacked bar shows cores
  and GPUs allocated / idle / unavailable, a bar chart why jobs are
  waiting (pending reason, CPU vs GPU), and a sortable table each
  partition's nodes, cores, GPUs, running and pending jobs (a job listing
  several partitions is pending in each — not additive).
- **Past 24 hours and past 7 days** (sacct, every partition): tiles and a
  summary table of jobs submitted (array tasks count individually),
  started, completed, **failed** (FAILED, OUT_OF_MEMORY, TIMEOUT,
  NODE_FAIL, BOOT_FAIL, DEADLINE), cancelled and other, and the mean /
  median / P90 **queue wait** — Eligible → Start, so time blocked by a
  dependency, a hold or an array throttle does not count as wait (the
  Partitions tab's columns measure Submit → Start). Charts: jobs per hour
  by outcome with the submitted line (All / CPU / GPU toggle, no fetch),
  cores held stacked by class and GPUs held against today's capacity,
  and hourly median queue wait (means one legend click away). A
  final-state table breaks the outcomes down, and an *Hourly data* table
  holds the numbers behind the charts. Windows are hour-aligned and
  include the current hour.
- **Never a fake zero** — a source that cannot be read renders as
  unavailable (a dead squeue is not an empty queue, a dead sacct is not
  an idle week), and history hours from a day chunk that failed are gaps,
  disclosed in a banner.
- **Refresh** re-reads the scontrol/squeue snapshots and re-assembles the
  history (its two edge day chunks re-fetch; full past days stay cached).
  A cold history load shows its day-chunk batch progress on the chip.

### Jobs tab
- Job table (top N by effective GPU-hours in the window, **N configurable**
  in the "Jobs to fetch" box, validated to 1–1000, default 100; the box is
  disabled while **Running only** is checked), window selection
  (24 h / 3 d / 7 d), and a **Running only** toggle (jobs with a live
  Prometheus GPU series). **Search** (job id / name) and the **partition**
  filter re-render the table client-side over the fetched rows — no
  fetch, no blur, and the efficiency charts are left untouched (they
  always show the fetched set's extremes). Results blur with a "Data is
  loading" popup only while a network fetch (window / running-only /
  fetch-limit changes, tab first visit) is in flight.
  Slurm array parents (Prometheus labels the work with the bare job ID)
  resolve their name/state/start/GPU allocation by merging **all** of the
  parent's physical task records (via `scontrol show job` while active,
  falling back to `sacct -j` task rows once finished) whose node lists
  intersect the observed nodes; tasks that ran elsewhere are excluded,
  and a parent with no node-matching task is left blank rather than
  misattributed.
- Clickable column sorting; click a row (or a bar in the chart) for a
  per-GPU utilization + VRAM time-series detail view with sacct metadata
  and job start/end markers.
- Efficiency charts: the 30 highest- and 30 lowest-average-efficiency jobs
  (average efficiency = mean utilization over the window, which — unlike
  effective GPU-hours — is not biased by job duration). The charts always
  show the extremes of the fetched set — search and partition filters
  change the table only, never the graphs.
- Job metadata is independent of the selected window: jobs that started
  before the chart window still show their name, state, true start, and
  GPU allocation (explicit sacct job-ID lookup, no visible-window date).
  Effective GPU-hours = allocated GPU-hours × mean utilization.

### Users tab
- User list aggregated per Slurm user over the window: job count, running
  job count, mean utilization, utilization-weighted GPU-hours, mean VRAM,
  and GPU types. The list is built from the same (TTL-cached) job-window
  queries as the Jobs tab — no sacct — so it loads cheaply.
- The **User** box filters the loaded list locally as you type — no fetch
  per keystroke. Pressing **Enter** (or clicking a table row) finalizes
  the selection; only then is that user's job list fetched — the window
  fetch is shared and unfiltered, the user filter is applied server-side
  in process (no per-user Prometheus query, so the request reuses the same
  cached fetch every tab reads) — and shown in the jobs card below (same
  enrichment and detail links as the Jobs tab). Raw text
  that matches no list entry is still sent, so admins can look up users
  with no GPU activity in the window.
- **Running only** hides users with no live job and re-fetches the
  selected user's running jobs.

### Partitions tab
- Per-Slurm-partition view: mean utilization per partition (time-weighted
  over the window), a utilization trend chart, a mean-occupancy chart
  (window average of allocated GPUs / resolved capacity per partition),
  and GPU capacity per partition.
- **Pending jobs by partition** (queue status): one `squeue -t PD` snapshot
  (30 s TTL cache) aggregated into the tab's GPU-group semantics — a MIG
  TresPerNode request forms its own group. Rows are per-partition
  *placements*: a job requesting several partitions appears in each, so a
  GPU job's per-node request (`squeue %b` × `%D` nodes) counts toward
  every row it could fill. A GPU job whose `%D` is N/A makes the exact
  GPU total `unknown` (the one-node lower bound stays in *GPUs (min)*);
  the header's pending-job count is the unique number of queued jobs. An
  squeue failure renders as an explicit unavailable state — never as an
  empty queue.
- **Running only** toggle restricts the bar, trend, and occupancy charts
  and the table to jobs with a live Prometheus GPU series.
- `GPUs` shows allocated/total: the total spans every scontrol node whose
  partition list contains the partition (idle capacity included); the
  allocated count is the exact per-partition live GPU count (a node shared
  by several partitions counts only the GPUs its jobs actually use).
- **Partition** selector: choosing one scopes the trend chart to that
  partition and the VRAM distribution below (server-side filter); the URL
  follows as `/partition/<name>` and restores on reload.
- **VRAM distribution by job**: a histogram of jobs binned by their
  average per-GPU peak VRAM (16 GB bins) over the window, weighted by
  allocated GPU-hours (sacct). The **Partition** selector filters the
  records server-side; the **Normalize** checkbox switches the bars to %
  of shown GPU-hours, and the dual **GPU utilization range** slider
  filters jobs by mean utilization client-side (no refetch). The card
  shares the tab's window and **Running only** controls. While its fetch
  is in flight the card shows its own loading popup — first
  "Loading VRAM history…", then real batch progress
  ("Enriching VRAM history: batch N of M…") while the sacct enrichment
  works through its 100-job batches. The other graphs and the table
  stay live and interactive.

### Nodes tab
- All GPU nodes with live utilization/VRAM (instant Prometheus query),
  GPU type/count, and the active jobs on each node.
- **Search** and **GPU type** filters, busy-only and GPU-nodes-only
  toggles; the snapshot time is shown in Europe/Helsinki.
- **refresh** forces a bypass of the 30-second scontrol/Prometheus cache.
- Click a row for the node's per-GPU utilization + VRAM time series,
  defaulting to **since job start** (earliest sacct start of the jobs
  actively reporting on that node; 1 h / 6 h / 24 h windows available).

### Deep links and cross-tab links
- Shareable URLs: `/job/<id>` (Jobs tab + job detail), `/node/<name>`
  (Nodes tab + node detail), `/partition/<name>` (Partitions tab scoped
  to that partition — trend and VRAM), `/user/<name>` (Users tab with
  that user's jobs fetched), plus `/status`, `/jobs`, `/partitions`,
  `/users`, `/nodes` for the plain tabs (plain `/partitions` clears any
  partition selection). `/` opens the Status tab.
- Cross-tab links: in the **Jobs** and **Users** job tables the **User**,
  **Partition**, and **Node** cells link to the Users, Partitions, and
  Nodes tabs respectively; in the **Nodes** tab each node name links to
  the node detail and each active job ID links to the job detail.
  Selecting a user, node, or partition anywhere keeps the URL in sync, so
  a view can be shared after a single click.

## Running

```console
$ cd /scratch/work/firoozh1/w/gpu-statistics
$ .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8090
# or: .venv/bin/python -m app
```

Then open <http://localhost:8090/>. Interactive API docs at `/docs`.

Requires Python 3.9+ with `fastapi`, `uvicorn`, `httpx` (see
`requirements.txt`; the project `.venv` already has them).

## Configuration

Prometheus connection settings are read in this order:

1. Environment: `PROM_URL`, `PROM_USER`, `PROM_PASSWORD`, `PROM_TIMEOUT`
2. `jobgraph.conf` — `$JOBGRAPH_CONFIG`, `/etc/jobgraph.conf`,
   `~/.config/jobgraph.conf` (shared with the jobgraph tool; keys
   `prom_url`, `username`, `password`, `timeout`)

Cluster access is **strictly read-only**: the app uses explicit `sacct -j`
lookups for job metadata, a bounded `sacct --allusers -X -S … -E …`
query for completed-job wait statistics, a bounded all-partition
`sacct --allusers -X -S … -E …` query per day for the Status tab's history,
`scontrol show nodes` / `show job`, `squeue -t PD`, and Prometheus read
queries.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | backend + Prometheus connectivity |
| `GET /api/jobs?since_hours=&user=&partition=&search=&limit=&running_only=&refresh=` | job table (Prometheus discovery + sacct enrichment; `running_only=true` keeps only jobs with a live GPU series; `refresh=true` bypasses the 60 s window cache) plus `efficiency_histogram` (GPU-hours by 10%-wide mean-utilization bucket, 0-100) |
| `GET /api/partitions/queue?since_hours=&running_only=` | live pending demand plus bounded completed-job wait metrics. `wait_history_coverage` reports accounting records examined, valid samples by GPU type, exclusions, failed batches, and completeness. |
| `GET /api/jobs/{jobid}?since_hours=` | per-GPU utilization/VRAM series + metadata (human-readable `start`/`end` preserved as-is) |
| `GET /api/partitions?since_hours=&running_only=` | utilization per GPU group + trend + `mean_occupancy` (window-average allocated share) + allocated/total GPU capacity; pending-job demand lives in `/api/partitions/queue`. A group is the Slurm partition, except MIG GPUs, which form their own group per node MIG GRES profile (`h200_3g.71gb`), so a MIG node never counts against its whole-GPU pool. Capacity is summed over all nodes of the group (idle included); a node shared by several partitions counts toward each |
| `GET /api/partitions/vram?since_hours=&running_only=&partition=` | per-job VRAM records for the distribution chart (average per-GPU peak VRAM in GB, mean utilization, allocated GPU-hours); `partition` keeps only one GPU group (a Slurm partition or a MIG GRES profile). Binning and the utilization-range filter happen client-side. Every VRAM-bearing candidate is returned (`total` = `len(jobs)`); the sacct enrichment reads the shared window-wide dump (day chunks, one retry per chunk, partial coverage disclosed through `enriched_frac`/`failed_batches`), and jobs it cannot enrich go through the per-ID row cache |
| `GET /api/partitions/vram/progress?since_hours=&running_only=&partition=` | transient batch progress `{done, total, failed_batches}` of the window's in-flight sacct dump, or `null` when nothing is in flight (finished, cached, or failed). Keyed by the window only — the enrichment reads the same window-wide dump as the queue's wait history, so this poll and the queue's progress poll return the same batch state |
| `GET /api/nodes?gpu_only=&refresh=` | node states (state/reason from `scontrol show node`) + live utilization/VRAM + active jobs (`refresh=true` bypasses the 30 s cache) |
| `GET /api/nodes/{name}?view=job_start\|1\|6\|24` | per-GPU utilization/VRAM series for one node (`job_start` = since the earliest active job started) |
| `GET /api/status/live?refresh=` | the cluster right now: `cpu`/`gpu` capacity (`alloc`, `idle`, `unavailable`, `total`, `occupancy_pct`), `nodes` (total, unavailable, by state), `running` and `pending` keyed `cpu`/`gpu` (pending: jobs, array tasks, eligible vs blocked, eligible age), `pending_reasons` and per-`partitions` rows. Each source has an `*_available` flag; its figures are null — never 0 — when it failed. `refresh=true` drops the 30 s scontrol/squeue snapshots |
| `GET /api/status/history?refresh=` | the past seven days, every partition: hourly `series` per class (submitted, completed/failed/cancelled/other, wait mean and P50, average cores and GPUs held; null for an hour no chunk covered), `summary[day\|week][cpu\|gpu\|all]` (counts, final `states`, `wait_avg_s`/`wait_p50_s`/`wait_p90_s` Eligible → Start), exact `windows`, `coverage` (failed chunks, stale running rows excluded) and today's `capacity`. `available: false` (still 200) when sacct could not be read at all. `refresh=true` re-assembles it; full past days stay cached |
| `GET /api/status/history/progress` | batch progress `{done, total, failed_batches}` of an in-flight history fetch (one batch per day chunk), or `null` |

Short in-memory TTL caches (20–300 s, at both the app and Prometheus-client
layers) avoid re-hitting the same query while the admin drags filters around.

## Data flow

Every external read has exactly one owner function in `sources.py`, each with
its own cache identity, TTL and single-flight: `TtlCache.get_or_set` makes the
first caller of a cold key run the fetch while later callers join its
in-flight result instead of starting their own. All external calls go through
`deps.*`, so the test seams stay in one place. What the windowed routes
actually pull for one window read:

- **Pinned windows** — `sources.pinned_window` pins the `(start, end, step)`
  triple for 60 s, so every tab opened within one TTL reads sources fetched
  for identical bounds, and each response reports the window its data
  actually covers.
- **Per-GPU raw series** — three Prometheus range queries per window
  (per-GPU utilization, mean VRAM %, peak VRAM GB), each cached 60 s and
  shared by every tab. The old per-tab PromQL aggregations are gone:
  `domain/views.py` derives the job and partition views from the raw series
  in process, memoized per window + scontrol fingerprint, so a tab change
  costs no upstream query at all.
- **Live snapshot** — `sources.live_snapshot` replaces the old five instant
  queries with two (the per-GPU utilization snapshot and the per-node VRAM
  average), cached 30 s; live IDs, per-node utilization, active jobs and
  allocation counts are derived from the pair. The `scontrol show nodes` /
  `show job` snapshots are cached 30 s alongside.
- **sacct** — one window-wide allocation dump, day-chunked on
  Europe/Helsinki midnights (GPU partitions only, resolved from the cached
  scontrol snapshot). Full past days are immutable and cached 1 h, shared
  across every window size covering them; the two edge chunks at the moving
  window bounds are always fetched fresh, and the assembled dump is cached
  300 s. The queue's wait history and the VRAM enrichment read the same
  dump; jobs it cannot enrich (e.g. array parents) fall back to a per-ID
  row cache (`KeyedBatchCache`, 300 s per ID, 1 h once terminal).
- **Status history** — `sources.status_history` reads the past seven days
  of every partition (`deps.sacct_status`, no `-r`) through the same
  day-chunk loader as the dump above (edges fresh, full days cached 1 h),
  but reduces each chunk to hourly CPU/GPU counters the moment it arrives:
  a week is ~200k rows, and only the counters are cached (300 s
  assembled). Each event — submit, start, end — counts in the chunk its
  timestamp falls in, so overlapping chunk results sum exactly without a
  JobID dedupe. sacct keeps stale "still running" rows for jobs that died
  without an accounting record; those are dropped against the live
  `scontrol show job` set (read alongside the chunks), with a 30-day age
  cut-off and a coverage flag when scontrol is down. A total sacct outage
  is never cached. The Status tab's live panel reads the shared scontrol
  snapshots plus a 30 s squeue snapshot (`sources.pending_jobs`; the
  Partitions queue keeps re-reading squeue per request).
- **Fan-out** — each route gathers its independent sources concurrently
  (`sources.gather`), so no window fetch waits on another.
- **Progress keys** — the dump's chunked fetch publishes
  `{done, total, failed_batches}` to `cache.progress_store` under one
  window-scoped key (`cache.vram_progress_key`) that both progress polls
  read, and the Status history under `cache.status_progress_key`; only
  the cache-miss leader writes a key, and a finished or failed fetch
  always clears it.

## Data semantics (important)

- The exporter publishes one utilization series **per GPU/MIG per job**.
  Job/partition "mean utilization" is the time-weighted mean of the
  observed device utilization samples.
- `sacct -j` enriches Prometheus-discovered job metadata. Completed-job wait
  statistics (and the VRAM enrichment) instead use the shared window-wide
  allocation dump — one bounded `sacct --allusers -X -S … -E …` query per
  Helsinki-day chunk of the window (see Data flow), so short completed jobs
  are not lost between Prometheus scrapes.
- The Status tab is cluster-wide, not GPU-only: a **GPU job** is one whose
  sacct ReqTRES or AllocTRES (squeue `tres_req_str`, scontrol AllocTRES)
  names at least one GPU, whatever partition it ran in; everything else
  is a CPU job. Its **queue wait** is Eligible → Start, while the
  Partitions tab's completed-job waits are Submit → Start — on Triton the
  two differ by several hours on average, because dependency chains and
  throttled arrays sit in the queue long before they are eligible.

## Tests

```console
$ .venv/bin/python -m pytest tests/ -q
```

Endpoint and parser tests: parsers (sacct/scontrol/prom shapes, edge
cases) and endpoints (fixed fake Prometheus + Slurm; asserts utilization
math is not trivially 100%, running-only filtering, GPU capacity joins,
job-start windows, and detail endpoints).
