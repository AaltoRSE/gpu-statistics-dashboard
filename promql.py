"""PromQL label-matcher construction.

Every place a label value gets interpolated into a query string goes
through here, so there is exactly one escaping convention instead of
the three the routes used to reinvent (manual replace(), re.escape(),
and — for path parameters — nothing at all).
"""

import re


def _escape(value):
    """Escape a value for a PromQL double-quoted string literal."""
    return (str(value).replace("\\", "\\\\")
            .replace('"', '\\"').replace("\n", "\\n"))


def label_eq(name, value):
    """A single ``name="value"`` matcher, with ``value`` escaped."""
    return '%s="%s"' % (name, _escape(value))


def label_in(name, values):
    """A ``name=~"^(?:a|b|...)$"`` matcher, exactly matching one of ``values``.

    Anchored on both ends so a value that happens to be a prefix or
    substring of another candidate can't cause a false match.
    """
    alternatives = "|".join(re.escape(str(v)) for v in sorted(values))
    return '%s=~"^(?:%s)$"' % (name, alternatives)


def selector(*matchers):
    """A ``{matcher1,matcher2,...}`` selector block from one or more
    matcher fragments (as returned by ``label_eq``/``label_in``).

    Returns ``""`` when no matchers are given, so it composes directly
    into ``"metric_name%s" % selector(...)`` whether or not there's a
    filter to apply.
    """
    return "{" + ",".join(matchers) + "}" if matchers else ""

def util_range(sel=""):
    """Range-filtered vector expression for GPU utilization samples.

    The Triton exporter occasionally publishes absurd utilization samples
    (observed: 1.4e16 among otherwise 0-100 readings), which poison every
    aggregation built on the raw metric — the Jobs GPU-hours-by-
    utilization histogram read ~14 trillion GPU-hours in its 90-100
    bucket, and the Partitions tab ranked v100_32gb's mean at ~1e12%.
    Utilization is a percentage, so every value consumed must lie in
    [0, 100]: this returns ``((metric{...} >= 0) and (metric{...} <= 100))``
    for use as the operand of an aggregating ``max by``/``sum by``/
    ``count by``, so malformed samples never enter an aggregation and
    numerators and denominators (occupancy counts, sample totals) stay
    consistent — a dropped sample vanishes from both sides. Use it for
    every query whose values are consumed; liveness/allocation counts
    (``count by (slurmjobid)``) keep the raw metric: they only ask
    whether a series exists.

    ``sel`` is a selector block as returned by selector() — compose the
    same way: ``"max by (...) %s" % util_range(sel)``.
    """
    metric = "slurm_job_utilization_gpu" + sel
    return "((%s >= 0) and (%s <= 100))" % (metric, metric)
