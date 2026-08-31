# Triton GPU Efficiency Dashboard

Live admin dashboard for tracking **jobs**, **partitions**, and **nodes** GPU
efficiency on the Triton cluster. A FastAPI backend collects data **on demand**
(queried live while the admin works with the dashboard — no periodic
collection) from:

- **`sacct`** — job metadata (name, user, state, start/end, GPU allocation)
- **`scontrol`** — node state and GPU capacity
- **Prometheus** (`stats.triton.aalto.fi`) — `slurm_job_*` exporter metrics:
  per-GPU utilization and VRAM, live and historical

The frontend is a plain-JS single page (Plotly.js via CDN) with three
interactive tabs.

A **light/dark theme** toggle sits in the header (top right). Dark is the
default; the light preference is remembered in `localStorage` and the
OS-level preference applies when no choice has been saved.

## Features

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
  the selection; only then is that user's job list fetched, server-side
  scoped by a `{user="…"}` Prometheus selector, and shown in the jobs
  card below (same enrichment and detail links as the Jobs tab). Raw text
  that matches no list entry is still sent, so admins can look up users
  with no GPU activity in the window.
- **Running only** hides users with no live job and re-fetches the
  selected user's running jobs.

### Partitions tab
- Per-Slurm-partition view: mean utilization per partition (time-weighted
  over the window), a utilization trend chart, a mean-occupancy chart
  (window average of allocated GPUs / resolved capacity per partition),
  and GPU capacity per partition.
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
  is in flight the card shows a "Data is loading" popup on its own — the
  other graphs and the table stay live and interactive.

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
  that user's jobs fetched), plus `/jobs`, `/partitions`, `/users`,
  `/nodes` for the plain tabs (plain `/partitions` clears any partition
  selection).
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
Cluster access is **strictly read-only**: the app only issues `sacct -j`,
`scontrol show nodes`, and Prometheus read queries.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | backend + Prometheus connectivity |
| `GET /api/jobs?since_hours=&user=&partition=&search=&limit=&running_only=&refresh=` | job table (Prometheus discovery + sacct enrichment; `running_only=true` keeps only jobs with a live GPU series; `refresh=true` bypasses the 60 s window cache) plus `efficiency_high` / `efficiency_low` (30 most/least average-efficient jobs) |
| `GET /api/jobs/{jobid}?since_hours=` | per-GPU utilization/VRAM series + metadata (human-readable `start`/`end` preserved as-is) |
| `GET /api/partitions?since_hours=&running_only=` | utilization per GPU group + trend + `mean_occupancy` (window-average allocated share) + allocated/total GPU capacity. A group is the Slurm partition, except MIG GPUs, which form their own group per node MIG GRES profile (`h200_3g.71gb`), so a MIG node never counts against its whole-GPU pool. Capacity is summed over all nodes of the group (idle included); a node shared by several partitions counts toward each |
| `GET /api/partitions/vram?since_hours=&running_only=&partition=` | per-job VRAM records for the distribution chart (average per-GPU peak VRAM in GB, mean utilization, allocated GPU-hours); `partition` keeps only one GPU group (a Slurm partition or a MIG GRES profile). Binning and the utilization-range filter happen client-side. `total` counts all candidates in the window; `jobs` holds only the top 2000 by effective GPU-hours, since a `sacct -j` over the whole window would time out |
| `GET /api/nodes?gpu_only=&refresh=` | node states (state/reason from `scontrol show node`) + live utilization/VRAM + active jobs (`refresh=true` bypasses the 30 s cache) |
| `GET /api/nodes/{name}?view=job_start\|1\|6\|24` | per-GPU utilization/VRAM series for one node (`job_start` = since the earliest active job started) |

Short in-memory TTL caches (20–300 s, at both the app and Prometheus-client
layers) avoid re-hitting the same query while the admin drags filters around.

## Data semantics (important)

- The exporter publishes one utilization series **per GPU/MIG per job**.
  Job/partition "mean utilization" is the time-weighted mean of the
  per-(job, node) **max** GPU utilization — i.e. "how busy", not aggregate
  GPU-hours. GPU-hours come from sacct allocation × elapsed time.
- `sacct` without `-j` is ACL-restricted to the caller's own jobs, so job
  discovery comes from Prometheus labels and `sacct -j <id>` is used for
  metadata.

## Tests

```console
$ .venv/bin/python -m pytest tests/ -q
```

Endpoint and parser tests: parsers (sacct/scontrol/prom shapes, edge
cases) and endpoints (fixed fake Prometheus + Slurm; asserts utilization
math is not trivially 100%, running-only filtering, GPU capacity joins,
job-start windows, and detail endpoints).
