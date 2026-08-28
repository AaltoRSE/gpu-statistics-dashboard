# Triton GPU Efficiency Dashboard

Live admin dashboard for tracking **jobs**, **partitions**, and **nodes** GPU
efficiency on the Triton cluster. A FastAPI backend collects data **on demand**
(queried live while the admin works with the dashboard — no periodic
collection) from:

- **`sacct`** — job metadata (name, user, state, start/end, GPU allocation)
- **`scontrol`** — node and partition state, node lists
- **Prometheus** (`stats.triton.aalto.fi`) — `slurm_job_*` exporter metrics:
  per-GPU utilization and VRAM, live and historical

The frontend is a plain-JS single page (Plotly.js via CDN) with three
interactive tabs.

## Features

### Jobs tab
- Job table (top 500 by effective GPU-hours in the window) with **search** by
  job id or job name, **user filter**, **partition filter**, and window
  selection (24 h / 3 d / 7 d).
- Clickable column sorting; click a row (or a bar in the chart) for a
  per-GPU utilization + VRAM time-series detail view with sacct metadata.
- Effective GPU-hours = allocated GPU-hours × mean utilization;
  efficiency = mean utilization of the job's GPUs.

### Partitions tab
- Mean utilization per group (partition or GPU type), time-weighted over the
  window, plus a utilization trend chart.
- Slurm node lists and states joined in from `scontrol show partitions`
  (Prometheus labels are shortened, e.g. `h200` → all `gpu-h200-*` partitions).

### Nodes tab
- All GPU nodes with live utilization/VRAM (instant Prometheus query),
  scontrol state, GPU type/count, and the active jobs on each node.
- **Search**, **state** and **GPU type** filters, busy-only toggle,
  GPU-nodes-only toggle.
- Click a row for the node's per-GPU utilization + VRAM time series
  (1 h / 6 h / 24 h).

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
`scontrol show nodes/partitions`, and Prometheus read queries.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | backend + Prometheus connectivity |
| `GET /api/jobs?since_hours=&user=&partition=&search=&limit=` | job table (Prometheus discovery + sacct enrichment) |
| `GET /api/jobs/{jobid}?since_hours=` | per-GPU utilization/VRAM series + metadata |
| `GET /api/partitions?since_hours=&group_by=partition\|gpu_type` | utilization by group + trend + scontrol node lists |
| `GET /api/nodes?gpu_only=` | node states + live utilization/VRAM + active jobs |
| `GET /api/nodes/{name}?window_hours=` | per-GPU utilization/VRAM series for one node |

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

26 tests: parsers (sacct/scontrol/prom shapes, edge cases) and endpoints
(fixed fake Prometheus + Slurm; asserts utilization math is not trivially
100%, filters, and detail endpoints).
