"""Parser unit tests. Run: .venv/bin/python -m pytest tests/ -q"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slurm import (  # noqa: E402
    SACCT_FIELDS,
    _parse_kv_block,
    _parse_sacct_row,
    parse_alloc_tres,
    parse_elapsed,
    parse_gres,
    parse_scontrol_nodes,
    parse_scontrol_partitions,
)
from config import _read_jobgraph_conf  # noqa: E402


def test_parse_elapsed_full():
    assert parse_elapsed("3-04:00:56") == 3 * 86400 + 4 * 3600 + 56


def test_parse_elapsed_short():
    assert parse_elapsed("00:12:34") == 12 * 60 + 34


def test_parse_elapsed_bad():
    assert parse_elapsed("Unknown") == 0
    assert parse_elapsed("") == 0


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
    assert gpu3["gpu_type"] == "v100"
    assert gpu3["cpus"] == 8
    assert gpu3["cpus_alloc"] == 2
    assert gpu3["free_mem"] == 150000
    assert gpu3["partitions"] == "gpu-v100-32g,gpu-debug"
    csl1 = nodes[1]
    assert csl1["gpus"] == 0
    assert csl1["gpu_type"] == ""


def test_parse_scontrol_nodes_free_mem_na():
    sample = "NodeName=fn3\n   State=IDLE\n   FreeMem=N/A RealMemory=N/A\n"
    nodes = parse_scontrol_nodes(sample)
    assert nodes[0]["free_mem"] == 0
    assert nodes[0]["real_mem"] == 0


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
        "19807768|train.sh|gomeze1|aalto_users|gpu-v100-32g|RUNNING"
        "|2026-08-25T14:32:59|Unknown|3-03:59:44"
        "|billing=64,cpu=8,gres/gpu:v100=2|gpu3|8".split("|")
    )
    assert row["JobID"] == "19807768"
    assert row["User"] == "gomeze1"
    assert row["AllocTRES"] == "billing=64,cpu=8,gres/gpu:v100=2"
    assert len(SACCT_FIELDS) == 12


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
