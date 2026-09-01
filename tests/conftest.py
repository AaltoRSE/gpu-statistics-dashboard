import pytest

import slurm


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
