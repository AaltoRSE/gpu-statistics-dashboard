"""Tests for gpu_groups.py: canonical GPU-type group resolution.

Every group key in the dashboard is a canonical GPU type (the short
scontrol GRES type; MIG profiles split out), resolved through
``canonical_gpu_type`` — not a Slurm partition name, which forks one
hardware pool into duplicate categories whenever a priority variant
(``...-ellis``) or a rename appears.
"""

import gpu_groups


def test_is_mig_gres_recognizes_profile_shapes():
    assert gpu_groups.is_mig_gres("h200_3g.71gb")
    assert gpu_groups.is_mig_gres("3g.70gb")  # bare Prometheus profile
    assert not gpu_groups.is_mig_gres("h100")
    assert not gpu_groups.is_mig_gres("")
    assert not gpu_groups.is_mig_gres(None)


def test_build_node_index():
    nodes = [{"name": "gpu1", "gpu_type": "h100"},
             {"name": "gpu2", "gpu_type": ""},
             {"name": "gpu3"}]
    assert gpu_groups.build_node_index(nodes) == {
        "gpu1": ["h100"], "gpu2": [], "gpu3": [],
    }


def test_build_node_index_prefers_full_gres_list():
    # gpu49: 4 whole H200 GPUs + 8 MIG h200_3g.71gb slices on one node —
    # every type must survive, not just gpu_type's first entry.
    nodes = [{"name": "gpu49", "gpu_type": "h200",
              "gres": [("h200", 4), ("h200_3g.71gb", 8)]}]
    assert gpu_groups.build_node_index(nodes) == {
        "gpu49": ["h200", "h200_3g.71gb"],
    }


def test_partition_gpu_types_maps_partitions_to_node_types():
    # The reverse index the pending queue resolves through: every
    # partition a GPU node lists maps to that node's GRES types; a
    # CPU-only partition is absent; priority variants converge on the
    # same type set.
    nodes = [
        {"name": "gpu1", "gpu_type": "h100", "gres": [("h100", 8)],
         "partitions": "gpu-h100,gpu-h100-ellis"},
        {"name": "gpu49", "gpu_type": "h200",
         "gres": [("h200", 4), ("h200_3g.71gb", 8)],
         "partitions": "gpu-h200,gpu-h200-ellis"},
        {"name": "csl1", "gpu_type": "", "gres": [],
         "partitions": "batch"},
        {"name": "gpu50", "gpu_type": "a100", "gres": [("a100", 4)],
         "partitions": "  gpu-a100 ,"},
    ]
    assert gpu_groups.partition_gpu_types(nodes) == {
        "gpu-h100": {"h100"},
        "gpu-h100-ellis": {"h100"},
        "gpu-h200": {"h200", "h200_3g.71gb"},
        "gpu-h200-ellis": {"h200", "h200_3g.71gb"},
        "gpu-a100": {"a100"},
    }


def test_partition_gpu_types_ignores_cpu_only_nodes():
    assert gpu_groups.partition_gpu_types(
        [{"name": "csl1", "gpu_type": "", "gres": [],
          "partitions": "batch"}]) == {}


def test_canonical_gpu_type_exact_match():
    assert gpu_groups.canonical_gpu_type("h200", ["h200", "a100"]) == "h200"
    assert gpu_groups.canonical_gpu_type("H200", ["h200", "a100"]) == "h200"


def test_canonical_gpu_type_mig_profile_resolution():
    # A MIG node's (sole) profile is authoritative regardless of the
    # series label; the bare exporter profile (3g.70gb) resolves to the
    # configured profile (h200_3g.71gb).
    assert gpu_groups.canonical_gpu_type("h200", ["h200_3g.71gb"]) == (
        "h200_3g.71gb")
    assert gpu_groups.canonical_gpu_type("3g.70gb", ["h200_3g.71gb"]) == (
        "h200_3g.71gb")
    assert gpu_groups.canonical_gpu_type(
        "3g.70gb", ["h200", "h200_3g.71gb"]) == "h200_3g.71gb"


def test_canonical_gpu_type_mixed_node_whole_label_stays_whole():
    # gpu49 carries both a whole H200 type and a MIG profile; a
    # whole-GPU series on it must never be dragged into the slice pool
    # (real exporter labels: "NVIDIA H200" for a whole GPU, "3g.70gb"
    # for a MIG slice).
    assert gpu_groups.canonical_gpu_type(
        "NVIDIA H200", ["h200", "h200_3g.71gb"]) == "h200"
    assert gpu_groups.canonical_gpu_type(
        "h200", ["h200", "h200_3g.71gb"]) == "h200"


def test_canonical_gpu_type_token_match_for_long_labels():
    # Long vendor labels resolve to the configured short types via
    # case-insensitive alphanumeric tokens.
    assert gpu_groups.canonical_gpu_type(
        "NVIDIA H200", ["h100", "h200"]) == "h200"
    assert gpu_groups.canonical_gpu_type(
        "Tesla V100-PCIE-32GB", ["a100", "v100"]) == "v100"
    assert gpu_groups.canonical_gpu_type(
        "NVIDIA A100-SXM4-80GB", ["a100"]) == "a100"


def test_canonical_gpu_type_sole_configured_whole_type():
    # A homogeneous fleet absorbs an unresolvable label; a MIG-shaped
    # label never falls into the whole-GPU pool.
    assert gpu_groups.canonical_gpu_type("", ["h100"]) == "h100"
    assert gpu_groups.canonical_gpu_type("NVIDIA H200", ["h100"]) == "h100"
    assert gpu_groups.canonical_gpu_type("3g.40gb", ["a100"]) == "3g.40gb"


def test_canonical_gpu_type_ambiguous_stays_visible():
    # A bare MIG label behind two configured profiles is ambiguous: keep
    # the normalized source label rather than pick arbitrarily.
    assert gpu_groups.canonical_gpu_type(
        "3g.70gb", ["h200_3g.71gb", "h200_4g.71gb"]) == "3g.70gb"


def test_canonical_gpu_type_missing_type_behavior():
    assert gpu_groups.canonical_gpu_type("", []) == "unknown"
    assert gpu_groups.canonical_gpu_type(None, None) == "unknown"
    assert gpu_groups.canonical_gpu_type("mystery", []) == "mystery"


def test_gpu_group_name_resolves_from_own_instance():
    metric = {"job": "gpu-h200", "gpu_type": "h200", "instance": "gpu49"}
    node_types = {"gpu49": ["h200", "h200_3g.71gb"]}
    assert gpu_groups.gpu_group_name(metric, node_types) == "h200"
    metric = {"job": "gpu-h200", "gpu_type": "3g.70gb", "instance": "gpu49"}
    assert gpu_groups.gpu_group_name(metric, node_types) == "h200_3g.71gb"


def test_gpu_group_name_whole_gpu_on_all_mig_node():
    # An all-MIG node's profile is authoritative even for a whole-shaped
    # label: the node has no whole-GPU pool to land in.
    metric = {"job": "gpu-h200", "gpu_type": "h200", "instance": "gpu49"}
    assert gpu_groups.gpu_group_name(
        metric, {"gpu49": ["h200_3g.71gb"]}) == "h200_3g.71gb"


def test_gpu_group_name_unindexed_instance_does_not_inherit_fleet():
    # An observed instance missing from the scontrol index must NOT be
    # absorbed into a sole fleet type: it resolves from its raw label.
    assert gpu_groups.gpu_group_name(
        {"job": "j", "gpu_type": "x", "instance": "ghost"},
        {"gpu1": ["h100"]}) == "x"
    # a job observed on unknown nodes likewise keeps its raw label.
    assert gpu_groups.job_gpu_group(
        {"nodes": ["ghost"], "partition": "p", "gpu_type": "x"},
        {"gpu1": ["h100"]}) == "x"


def test_gpu_group_name_falls_back_to_all_configured_types():
    # No (resolvable) instance label: the fleet union resolves bare
    # labels when it is unambiguous, and keeps the normalized label
    # otherwise.
    node_types = {"gpu1": ["h100"], "gpu2": ["h200"]}
    assert gpu_groups.gpu_group_name(
        {"job": "j", "gpu_type": "NVIDIA H200"}, node_types) == "h200"
    assert gpu_groups.gpu_group_name(
        {"job": "j", "gpu_type": "h100"}, {}) == "h100"
    assert gpu_groups.gpu_group_name(
        {"job": "j", "gpu_type": "3g.70gb"}, node_types) == "3g.70gb"


def test_gpu_group_name_missing_type_falls_back():
    # No label anywhere and no configured types: unknown.
    assert gpu_groups.gpu_group_name({"job": "j", "gpu_type": ""}, {}) == (
        "unknown")


def test_job_gpu_group_resolves_from_observed_nodes():
    job = {"nodes": ["gpu49"], "partition": "gpu-h200", "gpu_type": "h200"}
    node_types = {"gpu49": ["h200", "h200_3g.71gb"]}
    assert gpu_groups.job_gpu_group(job, node_types) == "h200"
    job = {"nodes": ["gpu49"], "partition": "gpu-h200", "gpu_type": "3g.70gb"}
    assert gpu_groups.job_gpu_group(job, node_types) == "h200_3g.71gb"


def test_job_gpu_group_without_observed_nodes_uses_fleet():
    # A job whose series were never observed on a known node still
    # canonicalizes against every configured type.
    job = {"nodes": [], "partition": "p", "gpu_type": "NVIDIA A100-SXM4-80GB"}
    assert gpu_groups.job_gpu_group(
        job, {"gpu1": ["a100"], "gpu2": ["h200"]}) == "a100"


def test_node_gpu_group_reports_parsed_gpu_type():
    # The node's own scalar type — the MIG profile for an all-MIG node;
    # partition membership is irrelevant. CPU-only nodes resolve to "".
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h200_3g.71gb", "partitions": "gpu-h200"}) == (
        "h200_3g.71gb")
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": "a,b"}) == "h100"
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "", "partitions": "batch"}) == ""
    assert gpu_groups.node_gpu_group({"gpu_type": None}) == ""
