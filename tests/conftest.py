import pytest
from test_app import client, fake_prom  # noqa: F401

import cache
import deps
import slurm
import sources

# The endpoint fixtures live in test_app; re-exporting them here lets
# sibling modules (test_shared_fetch) request them without importing the
# names themselves — a module-level import named like a test argument
# reads as a redefinition of it (ruff F811).

# The Groups tab's classification data (domain.org reads it via
# PROF_GROUPS_FILE): a synthetic stand-in for the real prof_groups.conf,
# so the suite never depends on that data file's contents. The two
# groups and one shared unit mirror the plan's examples and test_app's
# GROUP_MEMBERS member lists; a fresh copy per test also gives the file
# cache a fresh (path, mtime) key, so no test inherits another's parse.
TEST_PROF_GROUPS = """\
[schools]
T1 = CHEM | School of Chemical Engineering
T2 = ENG | School of Engineering
T3 = SCI | School of Science
T4 = ELEC | School of Electrical Engineering
T5 = Other | Legacy / university units
T6 = Other | Legacy / university units

[departments]
T300 = Department of Computer Science | T3
T410 = Department of Electrical Engineering and Automation | T4
T412 = Department of Information and Communications Engineering | T4

[units]
T21204 = Mechatronics | T212

[groups]
kyrkiv1 = Kyrki Ville | T410 | T40106
backstt1 = Bäckström Tom | T412 | T40571
"""


@pytest.fixture(autouse=True)
def _prof_groups_file(monkeypatch, tmp_path):
    """Point PROF_GROUPS_FILE at the synthetic conf above for every
    test; tests that need a different file override the env themselves
    (their setattr lands after this one)."""
    conf = tmp_path / "prof_groups.conf"
    conf.write_text(TEST_PROF_GROUPS, encoding="utf-8")
    monkeypatch.setenv("PROF_GROUPS_FILE", str(conf))


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="Regenerate tests/golden/*.json from the current API responses "
             "instead of comparing against them.",
    )


@pytest.fixture(autouse=True)
def _fresh_directory_cache():
    """Drop the directory (NSS) caches between tests.

    deps.directory_cache holds the Groups tab's member lists, per-user
    group lists, gid->name memo, membership index and classification —
    all keyed on real directory content a test may patch differently
    from its neighbour (fixtures that replace deps.route_cache with a
    fresh TtlCache replace this one too, but the autouse clear keeps
    every other test honest).
    """
    deps.directory_cache = cache.TtlCache(max_size=32768)


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
