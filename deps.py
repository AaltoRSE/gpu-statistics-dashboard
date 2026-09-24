"""External dependencies, accessed through one module instead of many.

Prometheus, sacct/scontrol, the wall clock, and the shared route cache
are the only things this app reads from outside its own process.
Every call site — regardless of which file it lives in — reaches them
through this module's attributes (``deps.get_prom()``,
``deps.sacct_jobs(...)``, ``deps.now()``, ``deps.route_cache``), never
by importing the underlying name directly.

That distinction matters for testing: ``from deps import sacct_jobs``
would bind a private copy of the reference in the importing module,
so a test patching ``deps.sacct_jobs`` would have no effect on code
that imported it that way. Patching stays effective — for any module,
present or future — only when call sites do ``import deps`` and then
``deps.sacct_jobs(...)``.
"""

import grp
import os
import pwd
import time as _time

from fastapi import HTTPException

from cache import TtlCache
from config import ConfigError, load_config
from prom import PromClient
from slurm import (  # noqa: F401 (re-exported)
    queue_pending,
    sacct_allocations,
    sacct_jobs,
    sacct_jobs_resilient,
    show_jobs,
    show_nodes,
)

route_cache = TtlCache()

_prom = None


class DirectoryError(Exception):
    """The NSS user directory failed (not merely an unknown user).

    ``user_groups`` returns None for a user the directory does not know
    — that is an answer. This is raised when the directory itself is
    unreachable (the underlying call raised OSError); the API maps it to
    a 502 so an org outage never reads as "everyone unaffiliated".
    """


def user_groups(username):
    """``groups <user>``: the user's NSS group names, or None for a user
    the directory does not know.

    Reads the same NSS/sssd sources the ``groups`` command does:
    ``pwd.getpwnam`` for the account and primary gid, ``os.getgrouplist``
    for every supplementary gid, and ``grp.getgrgid(g).gr_name`` for the
    group names. Returns None for an unknown user (distinct from a
    directory failure) and raises DirectoryError when NSS raises
    OSError. No caching here — callers cache the derived classification,
    whose TTL is theirs to choose.
    """
    try:
        pw = pwd.getpwnam(username)
        gids = os.getgrouplist(username, pw.pw_gid)
    except KeyError:
        return None
    except OSError as exc:
        raise DirectoryError(
            "could not list groups for %r: %s" % (username, exc)) from exc
    return sorted({grp.getgrgid(g).gr_name for g in gids})


def group_members(group_name):
    """The member usernames of one NSS group (``getent group <name>``),
    or None for a group the directory does not know.

    The Groups tab's membership index reads laitos-tNNNXX / tNNNXX-staff
    / tNNNXX-everyone for each configured research-group unit; one
    getgrnam per group, not one per user. Returns None for an unknown
    group (an answer — not every unit carries all three spellings) and
    raises DirectoryError when NSS raises OSError. No caching here —
    callers cache per group, whose TTL is theirs to choose.
    """
    try:
        gr = grp.getgrnam(group_name)
    except KeyError:
        return None
    except OSError as exc:
        raise DirectoryError(
            "could not list members of %r: %s" % (group_name, exc)) from exc
    return sorted(set(gr.gr_mem))


def get_prom():
    global _prom
    if _prom is None:
        try:
            cfg = load_config()
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        _prom = PromClient(
            cfg["api_base"], cfg["username"], cfg["password"], cfg["timeout"])
    return _prom


def now():
    """Current epoch seconds — the one clock read the app makes."""
    return _time.time()
