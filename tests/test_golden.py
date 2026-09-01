"""Golden response tests: pin the full JSON body of every API endpoint.

The endpoint tests in test_app.py assert *properties* of a response
(a field exists, a value is within range). They would not notice a
refactor that silently dropped a field, reordered a list, or changed
a rounding. These tests catch that: each case's full JSON body is
compared byte-for-byte against a saved fixture.

A diff here is not automatically wrong — it might be an intended
change. Review it, then regenerate deliberately:

    .venv/bin/python -m pytest tests/test_golden.py -q --update-golden

and commit the resulting tests/golden/*.json diff as part of the same
change, so the review sees both together.

Uses the same fake_prom/client fixtures as test_app.py (imported, not
duplicated) and the same fixed clock (NOW), so window/time fields stay
deterministic.
"""

import json
import os

import pytest
from test_app import client, fake_prom  # noqa: F401  (pytest fixtures)

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

# (case name, path, query params). Covers all six endpoints, plus a
# running_only=true and a partition= variant on the endpoints where
# those filters run meaningfully different code paths.
CASES = [
    ("jobs_default", "/api/jobs", {"since_hours": 24}),
    ("jobs_running_only", "/api/jobs",
     {"since_hours": 24, "running_only": "true"}),
    ("jobs_partition_filter", "/api/jobs",
     {"since_hours": 24, "partition": "gpu-h100"}),
    ("job_detail", "/api/jobs/1", {"since_hours": 24}),
    ("users", "/api/users", {"since_hours": 24}),
    ("partitions", "/api/partitions", {"since_hours": 24}),
    ("partitions_running_only", "/api/partitions",
     {"since_hours": 24, "running_only": "true"}),
    ("partitions_vram", "/api/partitions/vram", {"since_hours": 24}),
    ("partitions_vram_partition_filter", "/api/partitions/vram",
     {"since_hours": 24, "partition": "gpu-h100"}),
    ("nodes", "/api/nodes", {"gpu_only": "true"}),
    ("node_detail_job_start", "/api/nodes/gpu1", {"view": "job_start"}),
]


def _golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".json")


@pytest.mark.parametrize("name,path,params", CASES, ids=[c[0] for c in CASES])
def test_golden(client, name, path, params, request):  # noqa: F811
    resp = client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    actual = resp.json()
    golden_path = _golden_path(name)

    if request.config.getoption("--update-golden"):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(golden_path, "w") as fh:
            json.dump(actual, fh, indent=2, sort_keys=True)
            fh.write("\n")
        pytest.skip("regenerated " + golden_path)

    with open(golden_path) as fh:
        expected = json.load(fh)
    assert actual == expected, (
        "Response for '%s' no longer matches tests/golden/%s.json.\n"
        "If this is an intended change, review the diff and regenerate "
        "with:\n"
        "  .venv/bin/python -m pytest tests/test_golden.py -q "
        "--update-golden" % (name, name)
    )
