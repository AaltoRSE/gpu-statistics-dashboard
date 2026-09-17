"""GPU-group resolution: canonical GPU types, MIG profiles split out.

Every group key in the dashboard — utilization, capacity, occupancy,
historical waits, and the pending queue — is a canonical GPU type: the
short GRES type scontrol reports per node (``a100``, ``h100``, ``h200``,
``b300``, ``v100_16gb``, ``v100_32gb``, ``gh200``, and MIG profile names
such as ``h200_3g.71gb``), not a Slurm partition name. V100 GRES reports
only ``v100``; its node-local ``min-vram`` GRES supplies the memory suffix.
Partition names are unstable keys: priority-only variants (``...-ellis`` vs
``...-short``) target the same hardware, so grouping by them forks one pool into
duplicate queue categories and a rename would fork its history. GPU
types do not move.

A MIG-sliced GPU (``h200_3g.71gb``) must never count against its node's
whole-GPU capacity pool: a series, job, or node observed on a MIG-gres
node belongs to that profile's own group. Every place that answers
"which group does this belong to" — a Prometheus metric, a job, a node,
or a pending-job request — resolves through ``canonical_gpu_type`` here.
"""

import re

_MIG_GRES_RE = re.compile(r"^(?:[A-Za-z0-9]+_)?\d+[gm]\.\d+[gm]b?$",
                          re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


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


def partition_gpu_types(nodes):
    """``{partition name: {GPU types}}`` — the reverse of the node index.

    Every Slurm partition listed on a GPU node maps to that node's
    configured whole-GPU and MIG GRES types, so a pending job's raw
    partition list can be resolved to the hardware it could land on.
    Several priority partitions over the same nodes converge on the same
    type set (values are sets), and a CPU-only partition — no GPU node
    lists it — is absent entirely.
    """
    out = {}
    for n in nodes:
        gres = n.get("gres")
        types = [t for t, _ in gres] if gres else (
            [n["gpu_type"]] if n.get("gpu_type") else [])
        if not types:
            continue
        for p in (n.get("partitions") or "").split(","):
            p = p.strip()
            if p:
                out.setdefault(p, set()).update(types)
    return out


def _tokens(label):
    """Alphanumeric tokens of a casefolded label: ``Tesla V100-PCIE-32GB``
    -> {tesla, v100, pcie, 32gb}."""
    return set(_TOKEN_RE.findall(label.casefold()))


def canonical_gpu_type(gpu_type, configured_types):
    """The canonical GPU-type group for one raw exporter/Slurm label.

    ``configured_types`` are the scontrol GRES types of the hardware the
    label was observed on (one node's list, or the union over several
    observed nodes). Resolution is exact and ordered:

    1. exact case-insensitive match to a configured type — the common
       case, and the only one that preserves the configured spelling;
    2. the MIG-profile match: on an all-MIG node its (sole) profile is
       authoritative regardless of the series label; on a mixed
       whole+MIG node only a MIG-shaped label can resolve to a profile
       (a whole-GPU series must never be dragged into the slice pool),
       and a bare exporter profile (``3g.70gb``) resolves to the node's
       sole profile (``h200_3g.71gb``) when unambiguous;
    3. case-insensitive alphanumeric-token match, so long vendor labels
       (``NVIDIA H200``, ``Tesla V100-PCIE-32GB``) resolve to the
       configured short types (``h200``, ``v100``);
    4. a sole configured whole-GPU type absorbs an otherwise-unresolvable
       label on a homogeneous fleet — never a MIG-shaped label, which
       must stay separated from the whole-GPU pool;
    5. the normalized source label (stripped, casefolded) — or
       ``unknown`` when there is none.

    When several configured types remain plausible (e.g. two MIG profiles
    behind one bare label), the normalized source label is returned
    rather than an arbitrary pick, so the alias stays visible instead of
    silently merging into the wrong pool.
    """
    label = (gpu_type or "").strip()
    configured = [t for t in (configured_types or []) if t]
    folded = label.casefold()
    for t in configured:
        if t.casefold() == folded:
            return t
    mig_types = [t for t in configured if is_mig_gres(t)]
    if mig_types:
        has_whole = any(not is_mig_gres(t) for t in configured)
        # A whole-GPU label on a node that also carries MIG slices stays
        # whole; anything else MIG-resolves to the node's sole profile.
        if not (has_whole and not is_mig_gres(label)):
            if len(mig_types) == 1:
                return mig_types[0]
    if label:
        label_tokens = _tokens(label)
        matches = [t for t in configured
                   if _tokens(t) and _tokens(t) <= label_tokens]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return folded
    # A MIG-shaped label that survived every step above must never fall
    # into the whole-GPU pool below: keep it as its own (normalized) group.
    if label and is_mig_gres(label):
        return folded
    whole = [t for t in configured if not is_mig_gres(t)]
    if len(whole) == 1:
        return whole[0]
    return folded or "unknown"


def gpu_group_name(metric, node_gpu_types):
    """Canonical GPU-type group for one metric series.

    The series resolves against the scontrol types of its own node (the
    ``instance`` label). The fleet-wide union fallback applies only when
    the series carries NO observed instance at all; an observed but
    unindexed instance must NOT inherit the fleet's types (an unknown
    host's series would be silently absorbed into a sole fleet type) and
    resolves from its raw label instead.
    """
    node_gpu_types = node_gpu_types or {}
    inst = metric.get("instance", "")
    if not inst:
        configured = sorted({t for types in node_gpu_types.values()
                             for t in types})
    else:
        configured = node_gpu_types.get(inst, [])
    return canonical_gpu_type(metric.get("gpu_type", ""), configured)


def job_gpu_group(job, node_gpu_types):
    """Canonical GPU-type group for a job from its observed nodes.

    Resolves the job's own ``gpu_type`` label against the scontrol types
    of the nodes its series were observed on. The fleet-wide union
    fallback applies only when the job has no observed nodes at all; a
    job observed on unknown hosts resolves from its raw label.
    """
    node_gpu_types = node_gpu_types or {}
    types = {t for name in (job.get("nodes") or [])
             for t in (node_gpu_types.get(name) or [])}
    if not types and not (job.get("nodes") or []):
        types = {t for observed in node_gpu_types.values() for t in observed}
    return canonical_gpu_type(job.get("gpu_type") or "", sorted(types))


def node_gpu_group(node):
    """Canonical GPU-type group for one node: its parsed scalar
    ``gpu_type`` (the MIG profile for an all-MIG node). Nodes without a
    GPU type (CPU-only) resolve to ``""``; a mixed whole+MIG node reports
    its first GRES type here — per-type capacity accounting (in
    ``domain.partitions.gpu_capacity``) still attributes each of its
    GRES entries to its own group."""
    if not (node.get("gpu_type") or "").strip():
        return ""
    return node["gpu_type"]
