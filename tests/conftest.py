import os
import time

import pytest

import slurm

# The suite fixtures naive sacct timestamps ("2026-08-28T00:00:00") whose
# epoch values depend on the local zone (_sacct_epoch resolves them with
# datetime.fromisoformat().timestamp(), and sacct itself prints cluster-
# local time in production). Golden files bake the UTC interpretation to
# match CI; pinning the whole suite to UTC keeps them host-independent
# instead of requiring TZ=UTC on every dev machine.
os.environ["TZ"] = "UTC"
time.tzset()

def pytest_addoption(parser):
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="Regenerate tests/golden/*.json from the current API responses "
             "instead of comparing against them.",
    )


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Fail loudly, instead of silently shelling out, if a test reaches
    the real cluster.

    slurm._run() is the one place any sacct/scontrol subprocess gets
    invoked. Every test that needs Slurm data patches something above
    it — deps.sacct_jobs/show_jobs/show_nodes for endpoint tests, or
    slurm._run/_sacct_batch directly for parser tests — and a test's
    own patch (applied after this fixture, inside the test body)
    overrides this one for its duration. A test that forgets to patch
    anything gets a clear assertion error here instead of a hung or
    silently-real subprocess call.
    """
    def _boom(cmd, timeout=30):
        raise AssertionError(
            "test attempted a real subprocess call via slurm._run(%r) — "
            "patch deps.sacct_jobs/show_jobs/show_nodes, or slurm._run/"
            "_sacct_batch directly, for this test" % (cmd,))
    monkeypatch.setattr(slurm, "_run", _boom)
