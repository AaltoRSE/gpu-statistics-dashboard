"""Tests for gpu_groups.py: the MIG-aware partition-group resolution.

Moved out of test_app.py along with the functions themselves (was
app._node_gpu_group, etc.) and extended to cover the pair_aliases()
single-pass rewrite, which replaced an O(pairs * series) rescan.
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
        "gpu1": "h100", "gpu2": "", "gpu3": "",
    }


def test_gpu_group_name_mig_instance_beats_bare_job_label():
    metric = {"job": "gpu-h200", "gpu_type": "h200", "instance": "gpu49"}
    node_types = {"gpu49": "h200_3g.71gb"}
    assert gpu_groups.gpu_group_name(metric, node_types) == "h200_3g.71gb"


def test_gpu_group_name_falls_back_to_job_gtype_without_a_resolvable_node():
    # No instance label, and the gpu_type itself is a MIG profile: falls
    # back to "<job>_<gpu_type>" so it still stays separated from whole
    # GPUs of the same job/partition.
    metric = {"job": "gpu-h200", "gpu_type": "3g.70gb"}
    assert gpu_groups.gpu_group_name(metric, {}) == "gpu-h200_3g.70gb"


def test_gpu_group_name_plain_job_for_whole_gpu():
    metric = {"job": "gpu-h100", "gpu_type": "h100", "instance": "gpu1"}
    assert gpu_groups.gpu_group_name(metric, {"gpu1": "h100"}) == "gpu-h100"


def test_gpu_group_name_uses_alias_when_present():
    metric = {"job": "gpu-h200", "gpu_type": "h200"}
    aliases = {("gpu-h200", "h200"): "custom-alias"}
    assert gpu_groups.gpu_group_name(metric, {}, aliases) == "custom-alias"


def test_job_gpu_group_single_mig_profile_from_nodes():
    job = {"nodes": ["gpu49"], "partition": "gpu-h200", "gpu_type": "h200"}
    assert gpu_groups.job_gpu_group(job, {"gpu49": "h200_3g.71gb"}) == (
        "h200_3g.71gb")


def test_job_gpu_group_multiple_mig_profiles_join_under_partition():
    job = {"nodes": ["a", "b"], "partition": "gpu-h200", "gpu_type": ""}
    node_types = {"a": "h200_3g.71gb", "b": "h200_4g.71gb"}
    assert gpu_groups.job_gpu_group(job, node_types) == (
        "gpu-h200_h200_3g.71gb,h200_4g.71gb")


def test_job_gpu_group_plain_partition_for_whole_gpu():
    job = {"nodes": ["gpu1"], "partition": "gpu-h100", "gpu_type": "h100"}
    assert gpu_groups.job_gpu_group(job, {"gpu1": "h100"}) == "gpu-h100"


def test_node_gpu_group_first_nonempty_partition():
    # A node listed in two partitions: the first non-empty (trimmed)
    # partition wins. A leading-empty or comma-only value must not
    # yield "".
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": ",gpu-h100"}) == "gpu-h100"
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": "  , gpu-h100 ,"}) == "gpu-h100"
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": "a,b"}) == "a"
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": ""}) == ""
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h100", "partitions": ","}) == ""
    # A MIG node whose partition list also names the whole-GPU partition:
    # the MIG profile wins regardless.
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "h200_3g.71gb", "partitions": "gpu-h200"}) == (
        "h200_3g.71gb")
    assert gpu_groups.node_gpu_group(
        {"gpu_type": "", "partitions": "batch"}) == ""


def test_pair_aliases_single_pass_matches_per_pair_resolution():
    stats = [
        {"metric": {"job": "gpu-h100", "gpu_type": "h100", "instance": "gpu1"}},
        {"metric": {"job": "gpu-h200", "gpu_type": "h200", "instance": "gpu49"}},
    ]
    node_types = {"gpu1": "h100", "gpu49": "h200_3g.71gb"}
    aliases = gpu_groups.pair_aliases(stats, node_types)
    assert aliases == {
        ("gpu-h100", "h100"): "gpu-h100",
        ("gpu-h200", "h200"): "h200_3g.71gb",
    }


def test_pair_aliases_falls_back_when_a_pair_spans_two_mig_profiles():
    # The same (job, MIG-shaped gpu_type) pair observed via two different
    # node MIG profiles: ambiguous, so it must fall back to
    # gpu_group_name's <job>_<gpu_type> form rather than arbitrarily
    # picking one of the two profiles.
    stats = [
        {"metric": {"job": "gpu-h200", "gpu_type": "3g.70gb", "instance": "a"}},
        {"metric": {"job": "gpu-h200", "gpu_type": "3g.70gb", "instance": "b"}},
    ]
    node_types = {"a": "3g.70gb", "b": "4g.70gb"}
    aliases = gpu_groups.pair_aliases(stats, node_types)
    assert aliases[("gpu-h200", "3g.70gb")] == "gpu-h200_3g.70gb"


def test_pair_aliases_group_resolvable_only_from_observed_instances():
    # An instance with no scontrol node-type entry at all (missing from
    # node_types): must resolve purely from the metric's job/gpu_type
    # rather than raising or misclassifying as MIG.
    stats = [
        {"metric": {"job": "gpu-h100", "gpu_type": "h100", "instance": "ghost"}},
    ]
    aliases = gpu_groups.pair_aliases(stats, {})
    assert aliases == {("gpu-h100", "h100"): "gpu-h100"}
