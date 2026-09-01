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
