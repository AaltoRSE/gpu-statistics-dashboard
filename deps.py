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

import time as _time

from fastapi import HTTPException

from cache import TtlCache
from config import ConfigError, load_config
from prom import PromClient
from slurm import (  # noqa: F401 (re-exported)
    completed_jobs,
    queue_pending,
    sacct_allocations,
    sacct_jobs,
    sacct_jobs_resilient,
    show_jobs,
    show_nodes,
)

route_cache = TtlCache()

_prom = None


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
