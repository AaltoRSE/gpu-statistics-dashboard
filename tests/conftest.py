import pytest

import slurm
import sources


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="Regenerate tests/golden/*.json from the current API responses "
             "instead of comparing against them.",
    )


@pytest.fixture(autouse=True)
def _reset_shared_row_cache():
    """Drop the module-level per-ID sacct row cache between tests.

    The row cache deliberately outlives any single request (its entries
    are keyed per job ID), but a test suite must not inherit one test's
    patched sacct rows into the next.
    """
    sources.reset_caches()


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Fail loudly, instead of silently shelling out, if a test reaches
    the real cluster.

    slurm._run() is the one place any sacct/scontrol subprocess gets
    invoked. Every test that needs Slurm data patches something above
    it — deps.sacct_allocations/sacct_jobs_resilient/show_jobs/show_nodes
    for endpoint tests, or slurm._run/_sacct_batch directly for parser
    tests — and a test's own patch (applied after this fixture, inside
    the test body) overrides this one for its duration. A test that
    forgets to patch anything gets a clear assertion error here instead
    of a hung or silently-real subprocess call.
    """
    def _boom(cmd, timeout=30):
        raise AssertionError(
            "test attempted a real subprocess call via slurm._run(%r) — "
            "patch deps.sacct_allocations/sacct_jobs_resilient/show_jobs/"
            "show_nodes, or slurm._run/_sacct_batch directly, for this "
            "test" % (cmd,))
    monkeypatch.setattr(slurm, "_run", _boom)
