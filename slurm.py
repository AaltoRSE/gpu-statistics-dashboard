"""Slurm integration: sacct job enrichment and scontrol node/partition state.

Cluster note: ``sacct`` without ``-j`` is ACL-restricted to the caller's own
jobs, so job discovery comes from Prometheus labels and ``sacct -j <id>`` is
used for per-job metadata. All calls are read-only.
"""

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

SACCT_FIELDS = [
    "JobID",
    "JobName",
    "User",
    "Account",
    "Partition",
    "State",
    "Start",
    "End",
    "Elapsed",
    "AllocTRES",
    "NodeList",
    "NCPUS",
]

_NODE_BLOCK = re.compile(r"^NodeName=(\S+)")
_PART_BLOCK = re.compile(r"^PartitionName=(\S+)")
_KV = re.compile(r"^(\w+)=([^\s]*)")
_GPU_RES = re.compile(r"gpu:([\w.-]+):(\d+)|(?:^|,)gpu:(\d+)(?:,|$)")
_TRES_GPU = re.compile(r"gres/gpu(?::([\w.-]+))?=(\d+)")


class SlurmError(Exception):
    pass


def _run(cmd, timeout=30):
    if not shutil.which(cmd[0]):
        raise SlurmError("%s is not available" % cmd[0])
    try:
        proc = subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SlurmError(str(exc)) from exc
    if proc.returncode != 0:
        raise SlurmError(
            "%s exited %s: %s" % (cmd[0], proc.returncode, proc.stderr.strip()[:300])
        )
    return proc.stdout


def parse_elapsed(text):
    """Slurm elapsed ``3-04:00:56`` / ``00:12:34`` to seconds (int)."""
    text = (text or "").strip()
    if not text or text == "Unknown":
        return 0
    days = 0
    if "-" in text:
        days_part, text = text.split("-", 1)
        days = int(days_part)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[0], parts[1], parts[2]
    return days * 86400 + h * 3600 + m * 60 + s


def parse_alloc_tres(text):
    """AllocTRES string to (gpu_count, gpu_type)."""
    gpus, gpu_type = 0, ""
    for m in _TRES_GPU.finditer(text or ""):
        gpus = max(gpus, int(m.group(2)))
        if m.group(1):
            gpu_type = m.group(1)
    return gpus, gpu_type


def parse_gres(text):
    """Node Gres string to list of (gpu_type, count)."""
    if not text or text == "(null)":
        return []
    out = []
    for part in text.split(","):
        m = re.match(r"gpu:([\w.-]+):(\d+)", part.strip())
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def _split_hostlist(value):
    """Split a Slurm host list on commas outside brackets.

    ``gpu[01-03,07],dgx4`` -> ``["gpu[01-03,07]", "dgx4"]``; the comma of a
    node-range list belongs to the segment, not to the separator set.
    """
    segments, current, depth = [], [], 0
    for ch in value:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return [s for s in segments if s]


def expand_node_list(value):
    """Slurm node list to a set of node names.

    ``gpu[01-03,07-09]`` expands every comma-separated singleton or range,
    preserving the zero padding of each member. Plain hosts remain
    singletons; unsupported syntax (wildcards, strides) is retained as-is.
    """
    out = set()
    for segment in _split_hostlist((value or "").strip()):
        m = re.match(r"^([A-Za-z0-9]+)\[([0-9,-]+)\]$", segment)
        if not m:
            out.add(segment)
            continue
        prefix, members = m.groups()
        expanded = []
        try:
            for member in members.split(","):
                bounds = member.split("-")
                if len(bounds) == 1 and bounds[0].isdigit():
                    expanded.append(prefix + bounds[0])
                elif (len(bounds) == 2 and bounds[0].isdigit()
                      and bounds[1].isdigit()):
                    start, end = map(int, bounds)
                    width = len(bounds[0])
                    expanded.extend("%s%0*d" % (prefix, width, n)
                                    for n in range(start, end + 1))
                else:
                    raise ValueError(member)
        except ValueError:
            out.add(segment)
            continue
        out.update(expanded)
    return out


def _parse_kv_block(block_lines):
    """Parse all whitespace-delimited ``key=value`` tokens in scontrol lines."""
    fields = {}
    for line in block_lines:
        for key, value in re.findall(r"(\w+)=([^\s]+)", line):
            fields[key] = value
    return fields


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_scontrol_nodes(output):
    """Parse ``scontrol show nodes`` into a list of node dicts."""
    nodes, current = [], None
    for line in output.splitlines():
        m = _NODE_BLOCK.match(line)
        if m:
            current = {"name": m.group(1)}
            nodes.append(current)
            rest = line[m.end():].strip()
            if rest:
                current.update(_parse_kv_block([rest]))
        elif current is not None:
            current.update(_parse_kv_block([line]))
    parsed = []
    for node in nodes:
        gpus = parse_gres(node.get("Gres"))
        state = node.get("State", "UNKNOWN")
        parsed.append(
            {
                "name": node["name"],
                "state": state.split("+")[0],
                "state_full": state,
                "partitions": (node.get("Partitions") or "").strip(),
                "cpus": _int(node.get("CPUTot")),
                "gpus": gpus[0][1] if gpus else 0,
                "gpu_type": gpus[0][0] if gpus else "",
                "cpus_alloc": _int(node.get("CPUAlloc")),
                "free_mem": _int(node.get("FreeMem")),
                "real_mem": _int(node.get("RealMemory")),
            }
        )
    return parsed


def parse_scontrol_partitions(output):
    """Parse ``scontrol show partitions`` into a list of partition dicts."""
    parts, current = [], None
    for line in output.splitlines():
        m = _PART_BLOCK.match(line)
        if m:
            current = {"name": m.group(1)}
            parts.append(current)
            rest = line[m.end():].strip()
            if rest:
                current.update(_parse_kv_block([rest]))
        elif current is not None:
            current.update(_parse_kv_block([line]))
    for part in parts:
        part["state"] = part.get("State", "UNKNOWN")
        part["nodes"] = part.get("Nodes", "")
    return parts


def show_nodes():
    return parse_scontrol_nodes(_run(["scontrol", "show", "nodes"]))


def show_partitions():
    return parse_scontrol_partitions(_run(["scontrol", "show", "partitions"]))

def parse_scontrol_jobs(output):
    """Parse ``scontrol show job -o`` output into a job metadata dict.

    Returns ``{physical_jobid: metadata}`` where metadata uses the same
    lowercase shape as ``sacct_jobs`` rows, plus ``array_jobid`` and
    ``array_task_id``. Array parents have one record per physical task
    (``JobId=parent`` only for the parent's own row; tasks carry their own
    ``JobId`` and the shared ``ArrayJobId``).
    """
    jobs = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("JobId="):
            continue
        f = _parse_kv_block([line])
        jobid = f.get("JobId", "")
        if not jobid:
            continue
        state = f.get("JobState", "")
        end = f.get("EndTime", "")
        # Slurm projects EndTime for live jobs; only report it once the
        # job has actually ended.
        if state in ("RUNNING", "PENDING") or end == "Unknown":
            end = ""
        user = f.get("UserId", "")
        user = re.sub(r"\(\d+\)$", "", user)
        gpus, gpu_type = parse_alloc_tres(f.get("AllocTRES"))
        jobs[jobid] = {
            "jobid": jobid,
            "array_jobid": f.get("ArrayJobId", "") or "",
            "array_task_id": f.get("ArrayTaskId", "") or "",
            "name": f.get("JobName", "") or "",
            "user": user,
            "account": f.get("Account", "") or "",
            "partition": f.get("Partition", "") or "",
            "state": state,
            "start": f.get("StartTime", "") if f.get("StartTime", "") != "Unknown" else "",
            "end": end,
            "elapsed_s": parse_elapsed(f.get("RunTime", "")),
            "gpus": gpus,
            "gpu_type": gpu_type,
            "node_list": f.get("NodeList", "") or "",
            "ncpus": _int(f.get("NumCPUs")),
        }
    return jobs


def show_jobs():
    """All jobs currently known to the controller (read-only).

    ``scontrol show job -o`` cannot take a comma-separated ID list, so one
    call returns every job; callers filter the result instead of spawning
    a process per requested job.
    """
    return parse_scontrol_jobs(_run(["scontrol", "show", "job", "-o"]))


def _parse_sacct_row(parts):
    if len(parts) < len(SACCT_FIELDS):
        parts.extend([""] * (len(SACCT_FIELDS) - len(parts)))
    return dict(zip(SACCT_FIELDS, parts[: len(SACCT_FIELDS)]))


def _sacct_batch(job_ids, start_iso=None):
    cmd = [
        "sacct",
        "-j",
        ",".join(job_ids),
    ]
    if start_iso:
        cmd += ["-S", start_iso]
    cmd += [
        "-o",
        ",".join(SACCT_FIELDS),
        "--parsable2",
        "--noheader",
    ]
    out = _run(cmd, timeout=60)
    jobs = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        row = _parse_sacct_row(line.split("|"))
        jobid = row["JobID"]
        if "." in jobid:
            continue  # step/array rows
        if jobid not in jobs:
            jobs[jobid] = row
    return jobs


def sacct_jobs(job_ids, start_iso=None, workers=8):
    """Fetch metadata for many jobs. Returns {jobid: enriched dict}.

    ``start_iso`` is an optional ``-S`` date filter. Explicit job IDs already
    bound the request, so callers may omit it to retrieve jobs that started
    before the visible window.
    """
    job_ids = sorted(set(job_ids))
    if not job_ids:
        return {}
    batches = [job_ids[i : i + 100] for i in range(0, len(job_ids), 100)]
    results = {}
    warnings = []

    def fetch(batch):
        return _sacct_batch(batch, start_iso)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(fetch, batches):
            results.update(chunk)

    enriched = {}
    for jobid, row in results.items():
        gpus, gpu_type = parse_alloc_tres(row.get("AllocTRES"))
        try:
            ncpus = int(row.get("NCPUS") or 0)
        except ValueError:
            ncpus = 0
        enriched[jobid] = {
            "jobid": jobid,
            "name": row.get("JobName") or "",
            "user": row.get("User") or "",
            "account": row.get("Account") or "",
            "partition": row.get("Partition") or "",
            "state": row.get("State") or "",
            "start": row.get("Start") or "",
            "end": row.get("End") if row.get("End") != "Unknown" else "",
            "elapsed_s": parse_elapsed(row.get("Elapsed")),
            "gpus": gpus,
            "gpu_type": gpu_type,
            "node_list": row.get("NodeList") or "",
            "ncpus": ncpus,
        }
    return enriched
