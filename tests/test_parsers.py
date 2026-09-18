"""Parser unit tests. Run: .venv/bin/python -m pytest tests/ -q"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import _read_jobgraph_conf  # noqa: E402
from slurm import (  # noqa: E402
    SACCT_FIELDS,
    _parse_kv_block,
    _parse_sacct_row,
    expand_node_list,
    parse_alloc_tres,
    parse_elapsed,
    parse_gres,
    parse_scontrol_jobs,
    parse_scontrol_nodes,
    parse_scontrol_partitions,
    parse_squeue_json,
    parse_tres_per_node,
)


def test_expand_node_list_range_and_list():
    assert expand_node_list("gpu[01-03,07],dgx4") == {
        "gpu01", "gpu02", "gpu03", "gpu07", "dgx4"}



def test_expand_node_list_multiple_ranges():
    assert expand_node_list("gpu[01-03,07-09]") == {
        "gpu01", "gpu02", "gpu03", "gpu07", "gpu08", "gpu09"}

def test_expand_node_list_single_host():
    assert expand_node_list("gpu50") == {"gpu50"}
    assert expand_node_list("a[1],b") == {"a1", "b"}


def test_parse_elapsed_full():
    assert parse_elapsed("3-04:00:56") == 3 * 86400 + 4 * 3600 + 56


def test_parse_elapsed_short():
    assert parse_elapsed("00:12:34") == 12 * 60 + 34


def test_parse_elapsed_bad():
    assert parse_elapsed("Unknown") == 0
    assert parse_elapsed("") == 0
    # Malformed Slurm run times (e.g. RunTime: INVALID) degrade to 0.
    assert parse_elapsed("INVALID") == 0
    assert parse_elapsed("INVALID-04:00:56") == 0
    # More than three colon components is not a valid elapsed string.
    assert parse_elapsed("1:02:03:04") == 0


def test_parse_gres():
    assert parse_gres("gpu:v100:4(S:0-1),min-vram:no_consume:32G") == [("v100", 4)]
    assert parse_gres("(null)") == []
    assert parse_gres("") == []


def test_parse_alloc_tres():
    assert parse_alloc_tres("cpu=8,gres/gpu:h200=4") == (4, "h200")
    assert parse_alloc_tres("billing=64,cpu=8,gres/gpu=2") == (2, "")
    assert parse_alloc_tres("") == (0, "")


def test_parse_kv_block_multi_field():
    fields = _parse_kv_block(
        ["   State=MIXED+PLANNED ThreadsPerCore=1 TmpDisk=1500000"]
    )
    assert fields["State"] == "MIXED+PLANNED"
    assert fields["ThreadsPerCore"] == "1"
    assert fields["TmpDisk"] == "1500000"


def test_parse_scontrol_nodes_real_block():
    sample = """NodeName=gpu3 Arch=x86_64 CoresPerSocket=8
   CPUAlloc=2 CPUEfctv=8 CPUTot=8 CPULoad=0.5
   Gres=gpu:v100:4(S:0-1),min-vram:no_consume:32G
   State=MIXED+PLANNED ThreadsPerCore=1 TmpDisk=1500000 Weight=40
   Partitions=gpu-v100-32g,gpu-debug
   RealMemory=316000 AllocMem=314000 FreeMem=150000
NodeName=csl1 Arch=x86_64 CoresPerSocket=20
   CPUAlloc=28 CPUEfctv=40 CPUTot=40
   Gres=(null)
   State=MIXED ThreadsPerCore=1
   Partitions=batch-csl
   RealMemory=191000 AllocMem=190400 FreeMem=151219
"""
    nodes = parse_scontrol_nodes(sample)
    assert len(nodes) == 2
    gpu3 = nodes[0]
    assert gpu3["name"] == "gpu3"
    assert gpu3["state"] == "MIXED"
    assert gpu3["state_full"] == "MIXED+PLANNED"
    assert gpu3["gpus"] == 4
    assert gpu3["gpu_type"] == "v100_32gb"
    assert gpu3["cpus"] == 8
    assert gpu3["cpus_alloc"] == 2
    assert gpu3["free_mem"] == 150000
    assert gpu3["gres"] == [("v100_32gb", 4)]
    assert gpu3["partitions"] == "gpu-v100-32g,gpu-debug"
    csl1 = nodes[1]
    assert csl1["gpus"] == 0
    assert csl1["gpu_type"] == ""


def test_parse_scontrol_nodes_mixed_gres():
    # A node can carve part of its GPUs into MIG slices while leaving the
    # rest as whole GPUs (gpu49: 4 whole H200 + 8 MIG h200_3g.71gb
    # slices) — "gpus" must total every gres entry, not just the first,
    # and "gres" must keep the per-type breakdown.
    sample = (
        "NodeName=gpu49 Arch=x86_64 CoresPerSocket=32\n"
        "   CPUAlloc=48 CPUTot=128\n"
        "   Gres=gpu:h200:4(S:1),gpu:h200_3g.71gb:8(S:0),"
        "min-vram:no_consume:71G\n"
        "   State=MIXED ThreadsPerCore=2\n"
        "   Partitions=gpu-h200-71g-ia-ellis,gpu-h200-71g-ia\n"
    )
    node = parse_scontrol_nodes(sample)[0]
    assert node["gpus"] == 12
    assert node["gpu_type"] == "h200"
    assert node["gres"] == [("h200", 4), ("h200_3g.71gb", 8)]


def test_parse_scontrol_nodes_gpus_alloc_from_alloc_tres():
    # scontrol's own AllocTRES is authoritative for "currently allocated"
    # even when the node's monitoring exporter reports nothing at all
    # (gpu49 in reality: all 12 GPUs allocated across both GRES types).
    sample = (
        "NodeName=gpu49 Arch=x86_64\n"
        "   CPUAlloc=56 CPUTot=128\n"
        "   Gres=gpu:h200:4(S:1),gpu:h200_3g.71gb:8(S:0)\n"
        "   State=MIXED\n"
        "   AllocTRES=cpu=56,mem=431184M,gres/gpu=12,gres/gpu:h200=4,"
        "gres/gpu:h200_3g.71gb=8\n"
        "   Partitions=gpu-h200-71g-ia\n"
    )
    node = parse_scontrol_nodes(sample)[0]
    assert node["gpus_alloc"] == 12


def test_parse_scontrol_nodes_gpus_alloc_defaults_to_zero():
    sample = "NodeName=gpu3\n   State=IDLE\n   Gres=gpu:v100:4\n"
    node = parse_scontrol_nodes(sample)[0]
    assert node["gpus_alloc"] == 0


def test_parse_scontrol_nodes_free_mem_na():
    sample = "NodeName=fn3\n   State=IDLE\n   FreeMem=N/A RealMemory=N/A\n"
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["free_mem"] == 0
    assert nodes[0]["real_mem"] == 0


def test_parse_scontrol_nodes_reason_line():
    # scontrol show nodes reports the drain reason in its own field,
    # verbatim (colons inside the text must survive).
    sample = (
        "NodeName=gpu51 Arch=x86_64 CoresPerSocket=8\n"
        "   CPUAlloc=0 CPUTot=64\n"
        "   Gres=gpu:h200:8\n"
        "   State=IDLE+DRAIN ThreadsPerCore=1\n"
        "   Reason=maintenance: firmware update scheduled\n"
    )
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["state"] == "IDLE"
    assert nodes[0]["state_full"] == "IDLE+DRAIN"
    assert nodes[0]["reason"] == "maintenance: firmware update scheduled"


def test_parse_scontrol_nodes_embedded_reason():
    # scontrol show node -o appends the reason to the state instead.
    sample = (
        "NodeName=gpu7\n"
        "   Gres=gpu:h100:8\n"
        "   State=DOWN+DRAINED:firmware fault ThreadsPerCore=1\n"
    )
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["state"] == "DOWN"
    assert nodes[0]["state_full"] == "DOWN+DRAINED:firmware fault"
    assert nodes[0]["reason"] == "firmware fault"


def test_parse_scontrol_nodes_bare_qualifier_is_not_reason():
    # A qualifier without a colon is state, not an invented reason.
    sample = (
        "NodeName=fn9\n"
        "   Gres=(null)\n"
        "   State=IDLE+DRAIN ThreadsPerCore=1\n"
    )
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["state_full"] == "IDLE+DRAIN"
    assert nodes[0]["reason"] == ""


def test_parse_scontrol_nodes_reason_line_wins_over_embedded():
    # Both present: the explicit Reason= field is the authoritative one.
    sample = (
        "NodeName=gpu8\n"
        "   Gres=gpu:h200:8\n"
        "   State=IDLE+DRAIN\n"
        "   Reason=retiring: end of life\n"
    )
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["reason"] == "retiring: end of life"


def test_parse_scontrol_partitions():
    sample = """PartitionName=gpu-h200-71g-ia
   AllocNodes=ALL MaxNodes=16 MaxTime=24:00:00
   Nodes=gpu[12-15]
   State=UP TotalCPUs=1280 TotalNodes=4
PartitionName=interactive
   State=UP TotalCPUs=512 TotalNodes=8
"""
    parts = parse_scontrol_partitions(sample)
    assert len(parts) == 2
    assert parts[0]["name"] == "gpu-h200-71g-ia"
    assert parts[0]["nodes"] == "gpu[12-15]"
    assert parts[0]["state"] == "UP"
    assert parts[1]["nodes"] == ""


def test_parse_sacct_row():
    row = _parse_sacct_row(
        "19807768|19807768|train.sh|gomeze1|aalto_users|gpu-v100-32g|RUNNING"
        "|2026-08-25T14:32:59|2026-08-25T12:00:00|Unknown|3-03:59:44"
        "|billing=64,cpu=8,gres/gpu:v100=2|gpu3|8".split("|")
    )
    assert row["JobID"] == "19807768"
    assert row["JobIDRaw"] == "19807768"
    assert row["User"] == "gomeze1"
    assert row["Submit"] == "2026-08-25T12:00:00"
    assert row["AllocTRES"] == "billing=64,cpu=8,gres/gpu:v100=2"
    assert len(SACCT_FIELDS) == 14


def test_read_jobgraph_conf_bare(tmp_path):
    conf = tmp_path / "jobgraph.conf"
    conf.write_text(
        "prom_url = http://prom.example:9090\n"
        "username = read\n"
        "password = secret\n"
        "timeout = 45\n"
    )
    values = _read_jobgraph_conf(str(conf))
    assert values["prom_url"] == "http://prom.example:9090"
    assert values["username"] == "read"
    assert values["password"] == "secret"
    assert values["timeout"] == "45"


def test_read_jobgraph_conf_sectioned(tmp_path):
    conf = tmp_path / "jobgraph.conf"
    conf.write_text("[jobgraph]\nprom_url = http://x:9090\nusername = u\n")
    values = _read_jobgraph_conf(str(conf))
    assert values["prom_url"] == "http://x:9090"


def test_read_jobgraph_conf_missing(tmp_path):
    assert _read_jobgraph_conf(str(tmp_path / "nope.conf")) == {}


def test_sacct_batch_no_start_iso_omits_flag(monkeypatch):
    import slurm

    cmds = {}

    def fake_run(cmd, timeout=30):
        cmds["cmd"] = cmd
        return ""

    monkeypatch.setattr(slurm, "_run", fake_run)
    slurm._sacct_batch(["7"])
    assert "-S" not in cmds["cmd"]
    slurm._sacct_batch(["7"], start_iso="2020-01-01")
    assert "-S" in cmds["cmd"]
    assert cmds["cmd"][cmds["cmd"].index("-S") + 1] == "2020-01-01"


def test_sacct_batch_indexes_array_task_by_raw_id_too(monkeypatch):
    # A GPU-utilization series' slurmjobid label (and thus a caller's -j
    # lookup) is the raw numeric JobIDRaw, not sacct's own
    # "ArrayJobID_ArrayTaskID" notation for an array task: task 47 of array
    # 20001465 has JobID "20001465_47" but JobIDRaw "20008872" — sacct -j
    # 20008872 finds this exact row internally, but our own result dict
    # must be keyed so a caller who looked it up *by* "20008872" (the only
    # string they had) can find it, not just by "20001465_47".
    import slurm

    def fake_run(cmd, timeout=30):
        return "\n".join([
            "20001465_47|20008872|job_loop.sh|olkkonj1|aalto_users|"
            "gpu-v100-32g|COMPLETED|2026-08-31T11:43:55|2026-08-31T11:00:00|"
            "2026-09-01T02:50:54|15:06:59|gres/gpu:v100=1|gpu5|2",
            # Step rows carry the same JobIDRaw split with a dot suffix —
            # must still be filtered out, not indexed under a bogus key.
            "20001465_47.batch|20008872.batch|batch|||||||||||1",
        ])

    monkeypatch.setattr(slurm, "_run", fake_run)
    jobs = slurm._sacct_batch(["20008872"])
    assert set(jobs) == {"20001465_47", "20008872"}
    assert jobs["20008872"] is jobs["20001465_47"]
    assert jobs["20008872"]["User"] == "olkkonj1"
    assert jobs["20008872"]["Submit"] == "2026-08-31T11:00:00"


def test_sacct_batch_non_array_job_id_equals_raw_no_duplicate_key(monkeypatch):
    import slurm

    def fake_run(cmd, timeout=30):
        return ("20015894|20015894|train.sh|alice|acc|gpu-h100|RUNNING|"
                "2026-08-30T10:00:00|2026-08-30T09:30:00|Unknown|01:00:00|"
                "gres/gpu:h100=1|gpu1|8")

    monkeypatch.setattr(slurm, "_run", fake_run)
    jobs = slurm._sacct_batch(["20015894"])
    assert set(jobs) == {"20015894"}
    assert jobs["20015894"]["Submit"] == "2026-08-30T09:30:00"


def test_sacct_jobs_default_no_date(monkeypatch):
    import slurm

    seen = {}

    def fake_batch(job_ids, start_iso=None):
        seen["start_iso"] = start_iso
        return {}

    monkeypatch.setattr(slurm, "_sacct_batch", fake_batch)
    slurm.sacct_jobs(["7"])
    assert seen["start_iso"] is None


def test_completed_jobs_retries_chunks_and_retains_array_tasks(monkeypatch):
    import slurm

    commands, attempts = [], {}

    def fake_run(cmd, timeout=30):
        commands.append(cmd)
        start = cmd[cmd.index("-S") + 1]
        attempts[start] = attempts.get(start, 0) + 1
        if start == "2026-09-11T14:40:57" and attempts[start] == 1:
            raise slurm.SlurmError("temporary sacct timeout")
        return ("20001465_47|20008872|short|alice|acc|gpu-h200|COMPLETED|"
                "2026-09-17T14:40:57|2026-09-17T14:35:57|"
                "2026-09-17T14:45:57|00:05:00|gres/gpu:h200=1|gpu49|8")

    monkeypatch.setattr(slurm, "_run", fake_run)
    records, coverage = slurm.completed_jobs("2026-09-10T14:40:57",
                                             "2026-09-17T14:40:57")
    assert commands[0][:4] == ["sacct", "--allusers", "-X", "--state=COMPLETED"]
    assert attempts["2026-09-11T14:40:57"] == 2
    assert coverage == {"failed_batches": 0, "successful_batches": 7,
                        "complete": True}
    assert records == [{"jobid": "20001465_47", "name": "short",
                        "user": "alice", "account": "acc",
                        "partition": "gpu-h200", "state": "COMPLETED",
                        "submit": "2026-09-17T14:35:57",
                        "start": "2026-09-17T14:40:57",
                        "end": "2026-09-17T14:45:57", "elapsed_s": 300,
                        "gpus": 1, "gpu_type": "h200", "node_list": "gpu49",
                        "ncpus": 8}]


def test_completed_jobs_keeps_successful_chunks_after_failure(monkeypatch):
    import slurm

    def fake_run(cmd, timeout=30):
        if cmd[cmd.index("-S") + 1] == "2026-09-11T14:40:57":
            raise slurm.SlurmError("sacct timeout")
        return ("1|1|job|alice|acc|gpu-h200|COMPLETED|"
                "2026-09-10T15:00:00|2026-09-10T14:00:00|"
                "2026-09-10T16:00:00|01:00:00|gres/gpu:h200=1|gpu1|8")

    monkeypatch.setattr(slurm, "_run", fake_run)
    records, coverage = slurm.completed_jobs("2026-09-10T14:40:57",
                                             "2026-09-12T14:40:57")
    assert [r["jobid"] for r in records] == ["1"]
    assert coverage == {"failed_batches": 1, "successful_batches": 1,
                        "complete": False}


SCTRL_JOB_SAMPLE = (
    "JobId=100 JobName=train UserId=alice(1001) GroupId=alice(1001) Account=acc "
    "QOS=normal JobState=RUNNING NodeList=gpu1-2 NumNodes=2 NumCPUs=16 "
    "RunTime=01:02:03 StartTime=2026-08-30T10:00:00 EndTime=2026-08-31T10:00:00 "
    "Partition=gpu-h100 AllocTRES=cpu=16,gres/gpu:h100=4\n"
    "JobId=201 JobName=arr UserId=bob(1002) GroupId=bob(1002) Account=acc "
    "QOS=normal JobState=PENDING NodeList= NumNodes=1 NumCPUs=4 "
    "RunTime=00:00:00 StartTime=Unknown EndTime=Unknown Partition=batch "
    "AllocTRES=cpu=4\n"
    "JobId=202 ArrayJobId=201 ArrayTaskId=0-224 JobName=arr UserId=bob(1002) "
    "GroupId=bob(1002) Account=acc QOS=normal JobState=PENDING NodeList= "
    "NumNodes=1 NumCPUs=4 RunTime=00:00:00 StartTime=Unknown EndTime=Unknown "
    "Partition=batch AllocTRES=cpu=4\n"
)


def test_parse_scontrol_jobs_normal_job():
    jobs = parse_scontrol_jobs(SCTRL_JOB_SAMPLE)
    assert set(jobs) == {"100", "201", "202"}
    j = jobs["100"]
    assert j["jobid"] == "100"
    assert j["array_jobid"] == "" and j["array_task_id"] == ""
    assert j["name"] == "train"
    assert j["user"] == "alice"  # uid suffix stripped
    assert j["account"] == "acc"
    assert j["partition"] == "gpu-h100"
    assert j["state"] == "RUNNING"
    assert j["start"] == "2026-08-30T10:00:00"
    assert j["end"] == ""  # projected end hidden for RUNNING
    assert j["elapsed_s"] == 3723
    assert j["gpus"] == 4 and j["gpu_type"] == "h100"
    assert j["node_list"] == "gpu1-2"
    assert j["ncpus"] == 16


def test_parse_scontrol_jobs_array_tasks_share_parent():
    jobs = parse_scontrol_jobs(SCTRL_JOB_SAMPLE)
    p = jobs["201"]
    t = jobs["202"]
    assert p["array_jobid"] == "" or p["array_jobid"] == "201"
    assert t["array_jobid"] == "201"
    assert t["array_task_id"] == "0-224"
    # PENDING: no start/end, no runtime
    assert t["start"] == "" and t["end"] == "" and t["elapsed_s"] == 0
    assert t["node_list"] == ""


def test_show_jobs_command(monkeypatch):
    import slurm

    cmds = {}

    def fake_run(cmd, timeout=30):
        cmds["cmd"] = cmd
        return SCTRL_JOB_SAMPLE

    monkeypatch.setattr(slurm, "_run", fake_run)
    jobs = slurm.show_jobs()
    assert cmds["cmd"] == ["scontrol", "show", "job", "-o"]
    assert set(jobs) == {"100", "201", "202"}


def test_parse_scontrol_jobs_invalid_runtime():
    # A malformed RunTime (Slurm reports RunTime: INVALID for jobs that never
    # started) must degrade to 0 and must not abort the parse of the rest.
    sample = (
        "JobId=301 JobName=a UserId=carol(1003) GroupId=carol(1003) Account=acc "
        "QOS=normal JobState=PENDING NodeList= NumNodes=1 NumCPUs=4 "
        "RunTime=INVALID StartTime=Unknown EndTime=Unknown Partition=batch "
        "AllocTRES=cpu=4\n"
        "JobId=302 JobName=b UserId=dave(1004) GroupId=dave(1004) Account=acc "
        "QOS=normal JobState=RUNNING NodeList=gpu1 NumNodes=1 NumCPUs=8 "
        "RunTime=02:03:04 StartTime=2026-08-30T10:00:00 EndTime=2026-08-31T10:00:00 "
        "Partition=gpu-h100 AllocTRES=cpu=8,gres/gpu:h100=1\n"
    )
    jobs = parse_scontrol_jobs(sample)
    assert set(jobs) == {"301", "302"}
    assert jobs["301"]["elapsed_s"] == 0
    assert jobs["302"]["elapsed_s"] == 2 * 3600 + 3 * 60 + 4


def test_parse_tres_per_node_both_gres_forms():
    # squeue %b is the COLON-form TresPerNode; the equals-form AllocTRES
    # regex (parse_alloc_tres) cannot read it. Untyped and typed forms
    # differ by the type name starting with a letter.
    assert parse_tres_per_node("gres/gpu:1") == (1, "")          # untyped
    assert parse_tres_per_node("gres/gpu:a100:4") == (4, "a100")  # typed
    assert parse_tres_per_node("gres/gpu:h200:8") == (8, "h200")
    # non-GPU resources in the same string are ignored
    assert parse_tres_per_node("gres/gpu:1,gres/min-cuda-cc:80") == (1, "")
    assert parse_tres_per_node("gres/min-vram:40g") == (0, "")
    # a max-count entry's type wins
    assert parse_tres_per_node("gres/gpu:v100:1,gres/gpu:a100:4") == (4, "a100")
    # a digit-initial type name (the bare Prometheus MIG-profile form) is
    # grammatically indistinguishable from an untyped count and is
    # unsupported: it must NOT be misread as a count or a type
    assert parse_tres_per_node("gres/gpu:3g.40gb:2") == (0, "")
    # N/A / equals-form / empty parse as no GPU request
    assert parse_tres_per_node("N/A") == (0, "")
    assert parse_tres_per_node("gres/gpu=4") == (0, "")
    assert parse_tres_per_node("") == (0, "")


def _ts(epoch):
    """A squeue --json timestamp object; None encodes unset."""
    return ({"set": True, "infinite": False, "number": epoch}
            if epoch is not None
            else {"set": False, "infinite": False, "number": 0})


def _queue_json(jobs):
    return json.dumps({"jobs": jobs, "errors": [], "warnings": []})


def _sjob(jid, user, part, tpn="", tpj="", submit=1789635165,
          start=None, nodes=1, reason="Priority"):
    return {
        "job_id": jid, "user_name": user, "partition": part,
        "job_state": ["PENDING"],
        "submit_time": _ts(submit), "start_time": _ts(start),
        "state_reason": reason,
        "node_count": {"set": True, "infinite": False, "number": nodes},
        "tres_per_node": tpn, "tres_per_job": tpj,
    }


def test_parse_squeue_json_rows():
    # The four request shapes from the live cluster: typed tres-per-node,
    # none, untyped tres-per-node, and the TresPerJob-only bug shape
    # (constraints-only tres-per-node with the real gres/gpu:1 in
    # tres-per-job). A long partition list must survive verbatim (JSON
    # never truncates; fixed-width text columns did).
    long_part = "gpu-h100-80g,gpu-a100-80g,gpu-h200-141g-short"
    payload = _queue_json([
        _sjob(20276510, "jdoe", "gpu-a100-80g", "gres/gpu:a100:4",
              submit=1789632000, start=1789707600, nodes=2,
              reason="Resources"),
        _sjob(20291030, "jsmith", "batch-bdw", reason="Dependency"),
        _sjob(18162157, "jdoe", "gpu-a100-80g", "gres/gpu:1",
              submit=1780314039),
        _sjob(20300658, "bperson", long_part,
              "gres/min-vram:80g", "gres/gpu:1"),
    ])
    rows = parse_squeue_json(payload)
    assert [r["jobid"] for r in rows] == ["20276510", "20291030",
                                          "18162157", "20300658"]
    assert rows[0] == {
        "jobid": "20276510", "user": "jdoe", "partition": "gpu-a100-80g",
        "state": "PENDING",
        "submit": "2026-09-17T11:00:00", "start": "2026-09-18T08:00:00",
        "reason": "Resources", "nodes": 2, "gpus": 4, "gpu_type": "a100"}
    # unset start parses empty; no TRES anywhere parses zero GPUs
    assert rows[1]["user"] == "jsmith"
    # the untyped GRES form parses count without a type
    assert rows[2]["gpus"] == 1 and rows[2]["gpu_type"] == ""
    # the union: tres-per-node has no GPU, tres-per-job does — the
    # richer result wins, so the job is counted as real GPU demand
    assert rows[3]["gpus"] == 1 and rows[3]["gpu_type"] == ""
    # the full partition list survives
    assert rows[3]["partition"] == long_part
    assert parse_squeue_json('{"jobs": []}') == []
    assert parse_squeue_json("{}") == []


def test_parse_squeue_json_tres_union_precedence():
    # Both fields carry GPUs: higher count wins; equal counts prefer
    # the typed request.
    def row(tpn, tpj):
        return parse_squeue_json(_queue_json(
            [_sjob(100, "u", "gpu-h200", tpn, tpj)]))[0]
    j = row("gres/gpu:1", "gres/gpu:4")
    assert j["gpus"] == 4 and j["gpu_type"] == ""
    j = row("gres/gpu:1", "gres/gpu:h200:1")
    assert j["gpus"] == 1 and j["gpu_type"] == "h200"
    j = row("gres/gpu:h200:2", "gres/gpu:1")
    assert j["gpus"] == 2 and j["gpu_type"] == "h200"


def test_parse_squeue_json_timestamps_are_cluster_local():
    # squeue --json epochs are absolute; sacct prints Europe/Helsinki
    # naive strings and the dashboard header promises that wall clock.
    # The parse must be TZ-independent (no process-TZ leakage) and must
    # NOT read the epoch as UTC.
    # 1789635165 = 2026-09-17T08:52:45Z = 11:52:45 Helsinki.
    row = parse_squeue_json(_queue_json(
        [_sjob(20300658, "b", "gpu-h100", "gres/gpu:1",
               submit=1789635165, start=1789665248)]))[0]
    assert row["submit"] == "2026-09-17T11:52:45"
    assert row["start"] == "2026-09-17T20:14:08"
    # unset start stays ""
    row = parse_squeue_json(_queue_json(
        [_sjob(1, "b", "gpu-h100", "gres/gpu:1", start=None)]))[0]
    assert row["start"] == ""


def test_queue_pending_command(monkeypatch):
    import slurm

    cmds = {}

    def fake_run(cmd, timeout=30):
        cmds["cmd"] = cmd
        return '{"jobs": []}'

    monkeypatch.setattr(slurm, "_run", fake_run)
    assert slurm.queue_pending() == []
    assert cmds["cmd"] == ["squeue", "--json", "--all",
                           "--states=PENDING"]




def test_sacct_jobs_invalid_elapsed(monkeypatch):
    import slurm

    def fake_batch(job_ids, start_iso=None):
        return {
            "401": {
                "JobID": "401", "JobName": "c", "User": "erin", "Account": "acc",
                "Partition": "batch", "State": "PENDING",
                "Submit": "2026-08-30T08:00:00", "Start": "Unknown",
                "End": "Unknown", "Elapsed": "INVALID",
                "AllocTRES": "cpu=4", "NodeList": "", "NCPUS": "4",
            },
            "402": {
                "JobID": "402", "JobName": "d", "User": "frank", "Account": "acc",
                "Partition": "gpu-h100", "State": "RUNNING",
                "Submit": "2026-08-30T09:00:00",
                "Start": "2026-08-30T10:00:00", "End": "2026-08-31T10:00:00",
                "Elapsed": "00:05:06",
                "AllocTRES": "cpu=8,gres/gpu:h100=1", "NodeList": "gpu1", "NCPUS": "8",
            },
        }

    monkeypatch.setattr(slurm, "_sacct_batch", fake_batch)
    jobs = slurm.sacct_jobs(["401", "402"])
    assert set(jobs) == {"401", "402"}
    assert jobs["401"]["elapsed_s"] == 0
    assert jobs["401"]["submit"] == "2026-08-30T08:00:00"
    assert jobs["402"]["elapsed_s"] == 5 * 60 + 6
    assert jobs["402"]["submit"] == "2026-08-30T09:00:00"


def _contact_env(monkeypatch, tmp_path=None, conf_text=None, **env):
    """Run load_contact_config with an isolated environment/conf file."""
    import config
    for name in (
        "GARAGE_DIARY_PATH",
        "GARAGE_DIARY_REPO",
        "GARAGE_DIARY_CHECKOUT",
        "JOBGRAPH_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    if conf_text is not None:
        conf = tmp_path / "jobgraph.conf"
        conf.write_text(conf_text)
        monkeypatch.setenv("JOBGRAPH_CONFIG", str(conf))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return config.load_contact_config()


def test_contact_config_unconfigured_default(monkeypatch):
    result = _contact_env(monkeypatch)
    assert result["mode"] == "unconfigured"
    assert result["path"] == "" and result["repo"] == ""


def test_contact_config_local_env(monkeypatch):
    result = _contact_env(
        monkeypatch, GARAGE_DIARY_PATH=" /tmp/diary\n")
    assert result["mode"] == "local"
    assert result["path"] == "/tmp/diary"


def test_contact_config_remote_env_default_checkout(monkeypatch):
    result = _contact_env(monkeypatch, GARAGE_DIARY_REPO="git@x:r.git")
    assert result["mode"] == "remote"
    assert result["repo"] == "git@x:r.git"
    assert result["checkout"] == os.path.expanduser(
        "~/.cache/gpu-statistics/garagediary")


def test_contact_config_remote_env_checkout(monkeypatch):
    result = _contact_env(
        monkeypatch, GARAGE_DIARY_REPO="git@x:r.git",
        GARAGE_DIARY_CHECKOUT="/tmp/co")
    assert result == {
        "mode": "remote", "path": "", "repo": "git@x:r.git",
        "checkout": "/tmp/co"}


def test_contact_config_conf_file(monkeypatch, tmp_path):
    result = _contact_env(
        monkeypatch, tmp_path=tmp_path,
        conf_text="garage_diary_repo = conf-repo\n"
                  "garage_diary_checkout = conf-co\n")
    assert result == {
        "mode": "remote", "path": "", "repo": "conf-repo",
        "checkout": "conf-co"}


def test_contact_config_env_over_conf_file(monkeypatch, tmp_path):
    result = _contact_env(
        monkeypatch, tmp_path=tmp_path,
        conf_text="garage_diary_repo = conf-repo\n",
        GARAGE_DIARY_PATH="/env/path")
    assert result["mode"] == "local"
    assert result["path"] == "/env/path"


def test_contact_config_env_checkout_overrides_file_checkout_only(
        monkeypatch, tmp_path):
    # An env CHECKOUT must not clear the file's source key: the source
    # (repo) still comes from the file while the checkout comes from env.
    result = _contact_env(
        monkeypatch, tmp_path=tmp_path,
        conf_text="garage_diary_repo = conf-repo\n"
                  "garage_diary_checkout = conf-co\n",
        GARAGE_DIARY_CHECKOUT="/env/co")
    assert result == {
        "mode": "remote", "path": "", "repo": "conf-repo",
        "checkout": "/env/co"}


def test_contact_config_file_repo_with_env_checkout_default(
        monkeypatch, tmp_path):
    # No env checkout: the file's checkout applies untouched.
    result = _contact_env(
        monkeypatch, tmp_path=tmp_path,
        conf_text="garage_diary_repo = conf-repo\n"
                  "garage_diary_checkout = conf-co\n")
    assert result["checkout"] == "conf-co"


def test_contact_config_ambiguous_when_both_sources(monkeypatch):
    result = _contact_env(
        monkeypatch, GARAGE_DIARY_PATH="/tmp/diary",
        GARAGE_DIARY_REPO="git@x:r.git")
    assert result["mode"] == "ambiguous"
    assert result["path"] == "/tmp/diary"
    assert result["repo"] == "git@x:r.git"
