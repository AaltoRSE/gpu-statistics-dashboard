"""GPU-group resolution: the Slurm partition, except MIG GPUs.

A MIG-sliced GPU (``h200_3g.71gb``) must never count against its
node's whole-GPU capacity pool: a series, job, or node observed on a
MIG-gres node belongs to that profile's own group, not the bare
whole-GPU family. Every place that answers "which group does this
belong to" — a Prometheus metric, a job, a node, or a window's
(job, gpu_type) pair — resolves through the one MIG predicate here.
"""

import re

_MIG_GRES_RE = re.compile(r"^(?:[A-Za-z0-9]+_)?\d+[gm]\.\d+[gm]b?$",
                           re.IGNORECASE)


def is_mig_gres(name):
    """True when a GRES name is a MIG profile (``h200_3g.71gb``, or the bare
    Prometheus profile ``3g.70gb``) rather than a whole GPU."""
    return bool(_MIG_GRES_RE.match(name or ""))


def build_node_index(nodes):
    """``{node name: [scontrol GRES type, ...]}`` — every GPU/MIG type on
    the node, the lookup every resolver here needs to tell a MIG series
    from a whole-GPU one. A node may carry more than one type at once
    (e.g. part of its GPUs left whole, the rest carved into a MIG
    profile), so this is a list, not a single value."""
    out = {}
    for n in nodes:
        gres = n.get("gres")
        types = [t for t, _ in gres] if gres else (
            [n["gpu_type"]] if n.get("gpu_type") else [])
        out[n["name"]] = types
    return out


def _resolve_mig_type(gtype, ntypes):
    """The node's MIG type this series belongs to, or ``None``.

    ``ntypes`` is every GRES type scontrol reports for the series'
    node. When the node is entirely MIG, its (sole) profile is
    authoritative regardless of the series' own ``gpu_type`` label
    (some MIG series are observed with a generic/bare label). When the
    node also carries a whole-GPU type, that trust would misclassify a
    whole-GPU job sharing the node, so it only applies when this
    series' own label is itself MIG-shaped — matching a same-named
    node profile first, else the node's (sole) MIG profile.
    """
    mig_types = [t for t in ntypes if is_mig_gres(t)]
    if not mig_types:
        return None
    has_whole = any(not is_mig_gres(t) for t in ntypes)
    if has_whole and not is_mig_gres(gtype):
        return None
    return next((t for t in mig_types if t == gtype), mig_types[0])


def gpu_group_name(metric, node_gpu_types, aliases=None):
    """Canonical partition-view group name for one metric series.

    MIG GPUs must never merge into their node's whole-GPU pool: a series
    observed on a node whose scontrol GRES is a MIG profile belongs to that
    profile (``h200_3g.71gb``), not the bare family. A profile that cannot
    be resolved to a node falls back to ``<job>_<gpu_type>`` so it stays
    separated from whole GPUs; everything else keeps the Prometheus ``job``
    label (the Slurm partition).
    """
    job = metric.get("job", "") or ""
    gtype = metric.get("gpu_type", "") or ""
    if aliases is not None:
        alias = aliases.get((job, gtype))
        if alias:
            return alias
    inst = metric.get("instance", "")
    if inst:
        mig = _resolve_mig_type(gtype, node_gpu_types.get(inst) or [])
        if mig:
            return mig
    if is_mig_gres(gtype):
        return job + "_" + gtype if job else gtype
    return job or "unknown"


def job_gpu_group(job, node_gpu_types):
    """Canonical partition-view group for a job from its observed nodes."""
    job_gtype = job.get("gpu_type") or ""
    mig = set()
    for name in job.get("nodes") or []:
        found = _resolve_mig_type(job_gtype, node_gpu_types.get(name) or [])
        if found:
            mig.add(found)
    if not mig and is_mig_gres(job_gtype):
        mig.add(job_gtype)
    if not mig:
        return job.get("partition") or "unknown"
    if len(mig) == 1:
        return mig.pop()
    return (job.get("partition") or "unknown") + "_" + ",".join(sorted(mig))


def node_gpu_group(node):
    """Canonical partition-view group for one node.

    MIG-gres nodes (``gpu_type=h200_3g.71gb``) always belong to their
    profile group, even when their ``partitions`` field also lists the
    whole-GPU partition; everything else uses the first non-empty
    partition, which is the group the Partitions tab keys on. Nodes
    without a GPU type (CPU-only) resolve to ``""``.
    """
    if not (node.get("gpu_type") or "").strip():
        return ""
    if is_mig_gres(node["gpu_type"]):
        return node["gpu_type"]
    for p in (node.get("partitions") or "").split(","):
        p = p.strip()
        if p:
            return p
    return ""


def pair_aliases(stats, node_gpu_types):
    """``{(job, gpu_type): canonical group name}`` for every pair observed
    in a partition-summary ``stats`` series list.

    One canonical group name per (job, gpu_type) pair, derived from the
    summary series' instances so summary, trend, and occupancy agree. Scans
    ``stats`` once to collect each pair's MIG profiles, then resolves every
    pair from that map — an earlier version rescanned the whole series list
    once per pair, which was quadratic in the number of distinct pairs.
    """
    pairs = {(m.get("job", ""), m.get("gpu_type", "")) for m in
             (s["metric"] for s in stats)}
    pair_mig = {}
    for s in stats:
        m = s["metric"]
        inst = m.get("instance", "")
        gtype = m.get("gpu_type", "") or ""
        mig = _resolve_mig_type(gtype, node_gpu_types.get(inst) or []) \
            if inst else None
        if mig:
            key = (m.get("job", ""), gtype)
            pair_mig.setdefault(key, set()).add(mig)
    aliases = {}
    for pair in pairs:
        mig = pair_mig.get(pair)
        aliases[pair] = (
            next(iter(mig)) if mig and len(mig) == 1
            else gpu_group_name({"job": pair[0], "gpu_type": pair[1]},
                                 node_gpu_types)
        )
    return aliases
