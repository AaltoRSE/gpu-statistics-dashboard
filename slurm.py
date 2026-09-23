"""Slurm integration: sacct job enrichment and scontrol node/partition state.

Explicit ``sacct -j`` lookups enrich Prometheus-discovered jobs. Completed-job
wait metrics use one bounded ``sacct --allusers -X -S … -E …`` query, avoiding
scrape-sampling gaps and large concurrent ID batches. ``sacct_allocations``
fetches one chunk of the window-wide allocation dump the shared-source layer
(plan §3) assembles into per-day cached chunks. All calls are read-only.
"""

import datetime
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

SACCT_FIELDS = [
    "JobID",
    "JobIDRaw",
    "JobName",
    "User",
    "Account",
    "Partition",
    "State",
    "Start",
    "Submit",
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

_V100_VRAM_RE = re.compile(r"min-vram:no_consume:(16|32)G", re.IGNORECASE)


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
    try:
        days = 0
        if "-" in text:
            days_part, text = text.split("-", 1)
            days = int(days_part)
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        # Slurm reports malformed run times (e.g. ``RunTime: INVALID`` for
        # jobs that never started); treat them as "no elapsed time".
        return 0
    if len(parts) > 3:
        return 0
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
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


_REASON_RE = re.compile(r"(?:^|\s)Reason=(.*)$")
_STATE_RE = re.compile(r"(?:^|\s)State=(.*?)(?=\s+\w+=|$)")



def _v100_vram_gres(gres_text, gpus):
    """Name a homogeneous V100 pool by its configured VRAM capacity."""
    match = _V100_VRAM_RE.search(gres_text or "")
    if not match or not gpus or any(gpu_type != "v100" for gpu_type, _ in gpus):
        return gpus
    return [("v100_%sgb" % match.group(1), count) for _, count in gpus]
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
        # The state and the drain reason may contain spaces, which the
        # generic whitespace kv scan truncates, so capture the whole
        # field (up to the next ``key=`` token). The reason text is
        # kept verbatim (it may itself contain colons).
        if current is not None:
            sm = _STATE_RE.search(line)
            if sm:
                current["State"] = sm.group(1).strip()
            rm = _REASON_RE.search(line)
            if rm:
                reason = rm.group(1).strip()
                current["reason_full"] = "" if reason == "(null)" else reason
    parsed = []
    for node in nodes:
        gpus = _v100_vram_gres(node.get("Gres"), parse_gres(node.get("Gres")))
        gpus_alloc, _ = parse_alloc_tres(node.get("AllocTRES"))
        state = node.get("State", "UNKNOWN")
        # ``scontrol show node -o`` appends the drain reason to the state
        # (``DOWN+DRAINED:reason``); ``scontrol show nodes`` reports it in
        # a separate ``Reason=`` field, taken verbatim. Only a colon
        # inside the last qualifier is a reason: a bare extra qualifier
        # (``IDLE+DRAIN``) is state, not reason, and must not be invented
        # up.
        reason = node.get("reason_full", "")
        if not reason:
            parts = state.split("+")
            if len(parts) > 1 and ":" in parts[-1]:
                reason = parts[-1].split(":", 1)[1].strip()
        parsed.append(
            {
                "name": node["name"],
                "state": state.split("+")[0],
                "state_full": state,
                "reason": reason,
                "partitions": (node.get("Partitions") or "").strip(),
                "cpus": _int(node.get("CPUTot")),
                # A node's Gres line may list more than one GPU type (e.g. a
                # node with both whole GPUs and a MIG-sliced profile
                # carved from the rest); "gpus" is the total across every
                # type, and "gres" keeps the per-type breakdown so callers
                # that need to attribute capacity to the right group can.
                "gpus": sum(count for _, count in gpus),
                "gpu_type": gpus[0][0] if gpus else "",
                "gres": gpus,
                # scontrol's own live allocation count, straight from
                # AllocTRES's aggregate "gres/gpu=N" entry — authoritative
                # even when the node's monitoring exporter is down and
                # reports no per-GPU utilization series at all.
                "gpus_alloc": gpus_alloc,
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


def parse_tres_per_node(text):
    """A job's TresPerNode string (``squeue %b`` / scontrol's TresPerNode)
    to (gpu_count_per_node, gpu_type).

    This is the colon-form GRES syntax (``gres/gpu:a100:4``), not the
    equals-form AllocTRES ``parse_alloc_tres`` handles (``gres/gpu=4``).
    The untyped form (``gres/gpu:1``) and the typed form
    (``gres/gpu:a100:4``) are distinguished by the type name starting with
    a letter: a bare ``gres/gpu:1`` parses as count 1 with no type. This
    makes the grammar ambiguous for a digit-initial type name (the bare
    Prometheus MIG-profile form, ``gres/gpu:3g.40gb:2``): it cannot be
    told apart from an untyped count and is unsupported here — it parses
    as (0, "") rather than misreading the profile name as a number.
    Non-GPU resources in the same string (``gres/min-vram:40g``,
    ``gres/min-cuda-cc:80``) are ignored.
    """
    gpus, gpu_type = 0, ""
    for m in re.finditer(r"gres/gpu(?::([A-Za-z][\w.-]*))?(?::(\d+))?(?=,|$)",
                         text or ""):
        count = int(m.group(2)) if m.group(2) else 0
        if count > gpus:
            gpus = count
            gpu_type = m.group(1) or ""
    return gpus, gpu_type


def queue_pending():
    """Pending (PD) jobs from ``squeue --json``, hidden partitions included.

    JSON (not ``-o``/``-O`` text) because the GPU request can live in
    either of two fields — ``tres_per_node`` (the classic ``%b``) or
    ``tres_per_job`` (job-level, no ``-o`` short code; Slurm 25.11 keeps
    a constraints-only ``gres/min-vram:...`` request there alongside the
    real ``gres/gpu:1``) — and fixed-width text columns truncate long
    partition lists, which would silently drop eligibility types.
    ``--all`` is the code-side correction for a queue that is present
    but absent from the default view. Raises ``SlurmError`` when squeue
    is unavailable or fails; callers must surface that as an error
    state, not an empty queue.
    """
    return parse_squeue_json(_run(
        ["squeue", "--json", "--all", "--states=PENDING"], timeout=15))


def _tres_epoch(ts):
    """A squeue --json timestamp object to epoch seconds, or None."""
    if not isinstance(ts, dict) or not ts.get("set") or ts.get("infinite"):
        return None
    return ts.get("number")


def parse_squeue_json(payload):
    """Parse ``squeue --json`` output into pending-job dicts.

    The dict shape matches the historical ``-o`` parser exactly
    (``jobid``, ``user``, ``partition``, ``state``, ``submit`` ISO
    string, ``start`` ISO string or ``""``, ``reason``, ``nodes``,
    ``gpus``, ``gpu_type``) so callers are format-agnostic.

    GPU demand comes from BOTH TRES fields: a job may state its request
    as TresPerNode (``%b``, ``gres/gpu:2``) or TresPerJob
    (``gres/gpu:1``). Each is parsed with ``parse_tres_per_node`` (same
    colon grammar) and the richer result wins — higher count first,
    then the typed request at equal counts. Jobs naming no GPU anywhere
    stay ``gpus=0`` so the queue depth stays exact. A missing/unset
    start time stays ``""``.
    """
    data = json.loads(payload)
    jobs = []
    for j in data.get("jobs", []):
        node_gpus, node_type = parse_tres_per_node(j.get("tres_per_node") or "")
        job_gpus, job_type = parse_tres_per_node(j.get("tres_per_job") or "")
        if job_gpus > node_gpus or (job_gpus == node_gpus
                                    and job_type and not node_type):
            gpus, gpu_type = job_gpus, job_type
        else:
            gpus, gpu_type = node_gpus, node_type
        states = j.get("job_state") or [""]
        submit = _tres_epoch(j.get("submit_time"))
        start = _tres_epoch(j.get("start_time"))
        jobs.append({
            "jobid": str(j.get("job_id", "")),
            "user": j.get("user_name") or "",
            "partition": j.get("partition") or "",
            "state": states[0] if states else "",
            # squeue --json epochs are absolute; sacct and the dashboard
            # header render Europe/Helsinki-local naive strings, so
            # convert with the fixed cluster offset, never the process
            # TZ (host TZ varies, sacct does not).
            "submit": _cluster_iso(submit),
            "start": _cluster_iso(start),
            "reason": j.get("state_reason") or "",
            "nodes": (j.get("node_count") or {}).get("number", 0) or 0,
            "gpus": gpus,
            "gpu_type": gpu_type,
        })
    return jobs


CLUSTER_TZ = ZoneInfo("Europe/Helsinki")
"""The dashboard's display timezone (see static/index.html header)."""


def _cluster_iso(epoch):
    """Epoch seconds to a naive Europe/Helsinki ISO string, or ``""``.

    Matches sacct's output convention: sacct prints cluster-local
    naive timestamps regardless of the caller's TZ, so the queue's
    submit/estimated-start strings must use the same wall clock —
    never the process's local zone (deployment hosts vary).
    """
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, CLUSTER_TZ) \
        .replace(tzinfo=None).isoformat()


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
        start = f.get("StartTime", "")
        jobs[jobid] = {
            "jobid": jobid,
            "array_jobid": f.get("ArrayJobId", "") or "",
            "array_task_id": f.get("ArrayTaskId", "") or "",
            "name": f.get("JobName", "") or "",
            "user": user,
            "account": f.get("Account", "") or "",
            "partition": f.get("Partition", "") or "",
            "state": state,
            "start": start if start != "Unknown" else "",
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
        # A GPU-utilization series' slurmjobid label is the raw numeric
        # JobIDRaw, not sacct's own "ArrayJobID_ArrayTaskID" notation for
        # an array task (e.g. task 47 of array 20001465 has JobID
        # "20001465_47" but JobIDRaw "20008872" — the string Prometheus
        # and a caller's -j lookup both actually use). Index by both so a
        # raw-ID lookup resolves to the same row as the notation lookup;
        # a raw numeric string can never collide with a "_"-joined
        # notation key, so this never overwrites an unrelated job.
        raw = row.get("JobIDRaw")
        if raw and raw != jobid and raw not in jobs:
            jobs[raw] = row
    return jobs


def _enrich_sacct_row(row):
    """Convert one sacct row into the dashboard's job record shape."""
    gpus, gpu_type = parse_alloc_tres(row.get("AllocTRES"))
    return {
        "jobid": row.get("JobID") or "",
        # The raw numeric ID a Prometheus slurmjobid label (and thus a
        # per-ID row cache) actually keys on — for an array task it is
        # NOT derivable from "jobid" ("20001465_47"), so it must be
        # carried alongside (plan §1's per-ID sacct row cache).
        "jobid_raw": row.get("JobIDRaw") or "",
        "name": row.get("JobName") or "",
        "user": row.get("User") or "",
        "account": row.get("Account") or "",
        "partition": row.get("Partition") or "",
        "state": row.get("State") or "",
        "submit": row.get("Submit") or "",
        "start": row.get("Start") or "",
        "end": row.get("End") if row.get("End") != "Unknown" else "",
        "elapsed_s": parse_elapsed(row.get("Elapsed")),
        "gpus": gpus,
        "gpu_type": gpu_type,
        "node_list": row.get("NodeList") or "",
        "ncpus": _int(row.get("NCPUS")),
    }


def _completed_jobs_batch(start_iso, end_iso):
    """Completed allocation records from one bounded sacct interval."""
    cmd = [
        "sacct", "--allusers", "-X", "--state=COMPLETED",
        "-S", start_iso, "-E", end_iso,
        "-o", ",".join(SACCT_FIELDS), "--parsable2", "--noheader",
    ]
    out = _run(cmd, timeout=120)
    records = []
    for line in out.splitlines():
        row = _parse_sacct_row(line.strip().split("|"))
        if not line.strip() or "." in row.get("JobID", ""):
            continue
        records.append(_enrich_sacct_row(row))
    return records


def sacct_allocations(start_iso, end_iso, partitions):
    """All-user allocation records from one bounded sacct interval.

    One window chunk of the shared sacct dump (plan §3): unlike
    ``completed_jobs`` it takes no ``--state`` filter (wait history and
    VRAM enrichment both need every state) and takes an explicit
    partition list — the GPU partitions, resolved from the cached
    scontrol snapshot by the caller, so CPU jobs never enter the dump.
    An empty partition list omits ``-r`` (all partitions). Rows are
    parsed with the same parser/enricher as ``_completed_jobs_batch``
    (step rows skipped, blank lines skipped); ``SlurmError`` propagates
    from ``_run`` so the chunking layer can retry once and count the
    failure without discarding other chunks.
    """
    cmd = [
        "sacct", "--allusers", "-X",
        "-S", start_iso, "-E", end_iso,
        "-o", ",".join(SACCT_FIELDS), "--parsable2", "--noheader",
    ]
    if partitions:
        cmd[2:2] = ["-r", ",".join(partitions)]
    out = _run(cmd, timeout=120)
    records = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        row = _parse_sacct_row(line.split("|"))
        if "." in row.get("JobID", ""):
            continue  # step rows
        records.append(_enrich_sacct_row(row))
    return records


def completed_jobs(start_iso, end_iso, progress=None):
    """Completed records plus bounded-query completeness metadata.

    Daily chunks keep long all-user queries bounded. Each chunk retries
    once; successful chunks survive another chunk's failure, and inclusive
    boundary duplicates are removed by sacct JobID (array task IDs remain
    distinct). ``progress`` receives a callback after every chunk so a caller
    can surface batched progress instead of one opaque wait.
    """
    start = datetime.datetime.fromisoformat(start_iso)
    end = datetime.datetime.fromisoformat(end_iso)
    records, seen = [], set()
    failed_batches = successful_batches = 0
    cursor = start
    chunks = []
    while cursor < end:
        chunk_end = min(cursor + datetime.timedelta(days=1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    total = len(chunks)
    if progress:
        progress({"done": 0, "total": total, "failed_batches": 0})
    for index, (chunk_start, chunk_end) in enumerate(chunks):
        chunk_start_iso = chunk_start.isoformat(timespec="seconds")
        chunk_end_iso = chunk_end.isoformat(timespec="seconds")
        chunk = None
        try:
            for attempt in range(2):
                try:
                    chunk = _completed_jobs_batch(chunk_start_iso, chunk_end_iso)
                    break
                except SlurmError:
                    if attempt:
                        raise
        except SlurmError:
            failed_batches += 1
            if progress:
                progress({"done": index + 1, "total": total,
                          "failed_batches": failed_batches})
            continue
        successful_batches += 1
        for record in chunk:
            if record["jobid"] not in seen:
                seen.add(record["jobid"])
                records.append(record)
        if progress:
            progress({"done": index + 1, "total": total,
                      "failed_batches": failed_batches})
    return records, {"failed_batches": failed_batches,
                     "successful_batches": successful_batches,
                     "complete": failed_batches == 0}


def sacct_jobs(job_ids, start_iso=None, workers=8):
    """Fetch metadata for many jobs. Returns {jobid: enriched dict}.

    ``start_iso`` is an optional ``-S`` date filter. Explicit job IDs already
    bound the request, so callers may omit it to retrieve jobs that started
    before the visible window.
    """
    enriched, _ = sacct_jobs_resilient(job_ids, start_iso, workers)
    return enriched


def sacct_jobs_resilient(job_ids, start_iso=None, workers=8, progress=None):
    """``sacct_jobs`` plus failed-batch accounting, for callers that
    disclose partial coverage instead of failing the whole enrichment.

    Each 100-ID batch retries once; a batch that still fails is counted in
    the returned tuple instead of discarding every other batch's records
    (one slow slurmdbd response must not 502 a 2000-job enrichment).
    ``progress`` receives ``{"done", "total", "failed_batches"}`` before
    the first batch (``done=0``) and once per finished batch — the same
    batched-progress contract as ``completed_jobs``.
    """
    job_ids = sorted(set(job_ids))
    if not job_ids:
        return {}, 0
    batches = [job_ids[i : i + 100] for i in range(0, len(job_ids), 100)]
    total = len(batches)
    results = {}
    failed_batches = 0
    if progress:
        progress({"done": 0, "total": len(batches),
                  "failed_batches": 0})

    def fetch(batch):
        # Failure is reported as the (rows, failed) pair instead of
        # mutating shared counters from the worker threads.
        try:
            for attempt in range(2):
                try:
                    return _sacct_batch(batch, start_iso), False
                except SlurmError:
                    if attempt:
                        raise
        except SlurmError:
            return {}, True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, batch) for batch in batches]
        # as_completed: `done` must advance as soon as any batch finishes,
        # not only when input order reaches it.
        for done, future in enumerate(as_completed(futures), 1):
            chunk, failed = future.result()
            failed_batches += int(failed)
            results.update(chunk)
            if progress:
                progress({"done": done, "total": total,
                          "failed_batches": failed_batches})
    enriched = {}
    for jobid, row in results.items():
        enriched[jobid] = _enrich_sacct_row(row)
    return enriched, failed_batches

