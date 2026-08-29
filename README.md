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

## Features

### Jobs tab
- Job table (top 500 by effective GPU-hours in the window) with **search** by
  job id or job name, **user filter**, **partition filter**, window
  selection (24 h / 3 d / 7 d), and a **Running only** toggle (jobs with a
  live Prometheus GPU series).
- Clickable column sorting; click a row (or a bar in the chart) for a
  per-GPU utilization + VRAM time-series detail view with sacct metadata
  and job start/end markers.
- Effective GPU-hours = allocated GPU-hours × mean utilization;
  efficiency = mean utilization of the job's GPUs.

### Partitions tab
- GPU-type view: mean utilization per GPU type (time-weighted over the
  window), a utilization trend chart, and GPU capacity per type.
- **Running only** toggle restricts both charts and the table to jobs with
  a live Prometheus GPU series.
- `GPUs` shows allocated/total: when a group's nodes all resolve to one
  scontrol GPU type the total spans every node of that type (idle capacity
  included), otherwise only the observed instances count.

### Nodes tab
- All GPU nodes with live utilization/VRAM (instant Prometheus query),
  GPU type/count, and the active jobs on each node.
- **Search** and **GPU type** filters, busy-only and GPU-nodes-only
  toggles; the snapshot time is shown in Europe/Helsinki.
- **refresh** forces a bypass of the 30-second scontrol/Prometheus cache.
- Click a row for the node's per-GPU utilization + VRAM time series,
  defaulting to **since job start** (earliest sacct start of the jobs
  actively reporting on that node; 1 h / 6 h / 24 h windows available).

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
| `GET /api/jobs?since_hours=&user=&partition=&search=&limit=&running_only=` | job table (Prometheus discovery + sacct enrichment; `running_only=true` keeps only jobs with a live GPU series) |
| `GET /api/jobs/{jobid}?since_hours=` | per-GPU utilization/VRAM series + metadata (incl. parsed `start_epoch`/`end_epoch`) |
| `GET /api/partitions?since_hours=&running_only=` | utilization per GPU type + trend + allocated/total GPU capacity |
| `GET /api/nodes?gpu_only=&refresh=` | node states + live utilization/VRAM + active jobs (`refresh=true` bypasses the 30 s cache) |
| `GET /api/nodes/{name}?view=job_start\|1\|6\|24` | per-GPU utilization/VRAM series for one node (`job_start` = since the earliest active job started) |

Short in-memory TTL caches (30–300 s) avoid re-hitting the same query while
the admin drags filters around; every interaction still fetches live data.

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
