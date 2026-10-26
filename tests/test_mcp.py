"""MCP endpoint tests: the tools against the same fake Prometheus + Slurm
+ NSS the endpoint tests use (in-memory MCP client), the bearer-token
gate in front of /mcp/, and one call over real Streamable HTTP.

Run: .venv/bin/python -m pytest tests/test_mcp.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp import Client  # noqa: E402

import app as appmod  # noqa: E402
import deps  # noqa: E402
from api import mcp_server  # noqa: E402
from slurm import SlurmError  # noqa: E402

TOOLS = {
    "cluster_status", "cluster_history", "list_jobs", "job_detail",
    "list_users", "user_priority", "list_groups", "group_members",
    "partition_utilization", "partition_queue", "list_nodes", "node_detail",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(name, args=None):
    """One tool call over a fresh in-memory MCP connection."""
    async with Client(mcp_server.mcp, raise_exceptions=True) as c:
        return await c.call_tool(name, args or {})


@pytest.mark.anyio
async def test_every_tool_is_listed_read_only():
    async with Client(mcp_server.mcp, raise_exceptions=True) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations.read_only_hint for t in tools)
    # an agent must never bypass the source caches
    assert not any("refresh" in t.input_schema["properties"] for t in tools)


@pytest.mark.anyio
async def test_list_jobs_matches_the_http_route(client):
    http = client.get("/api/jobs", params={"since_hours": 24, "limit": 20})
    r = await call("list_jobs")
    assert not r.is_error
    jobs = r.structured_content["jobs"]
    assert [j["jobid"] for j in jobs] == [
        j["jobid"] for j in http.json()["jobs"]]
    # the response model filters the job view's internal keys out
    assert not any(k.startswith("_") for j in jobs for k in j)


@pytest.mark.anyio
async def test_job_detail_summarizes_series_unless_asked(client):
    r = await call("job_detail", {"jobid": "1"})
    body = r.structured_content
    assert "series" not in body
    util = body["series_summary"]["utilization"]
    assert util and {"metric", "samples", "mean", "max", "last"} <= set(util[0])
    assert body["mean_util"] == client.get("/api/jobs/1").json()["mean_util"]

    raw = (await call("job_detail", {"jobid": "1", "include_series": True}))
    assert raw.structured_content["series"] == \
        client.get("/api/jobs/1").json()["series"]


@pytest.mark.anyio
async def test_cluster_history_drops_the_hourly_series_by_default(client):
    body = (await call("cluster_history")).structured_content
    assert body["available"] is True and body["summary"]
    assert "series" not in body
    hourly = (await call("cluster_history",
                         {"include_hourly": True})).structured_content
    assert hourly["series"]["t"]


@pytest.mark.anyio
async def test_list_groups_limit_and_school_filter(client):
    http = client.get("/api/groups", params={"since_hours": 24}).json()
    body = (await call("list_groups", {"limit": 1})).structured_content
    assert body["count"] == http["count"]  # before the limit
    assert [r["group_id"] for r in body["groups"]] == [
        http["groups"][0]["group_id"]]
    assert "members" not in body["groups"][0]  # internal, model-filtered

    elec = (await call("list_groups", {"school": "elec"})).structured_content
    assert {r["school_code"] for r in elec["groups"]} == {"ELEC"}
    assert elec["count"] == len(elec["groups"]) == sum(
        r["school_code"] == "ELEC" for r in http["groups"])


@pytest.mark.anyio
async def test_group_members_unknown_group_is_a_tool_error(client):
    r = await call("group_members", {"group_id": "nobody"})
    assert r.is_error
    assert "no such group" in r.content[0].text


@pytest.mark.anyio
async def test_source_failures_are_readable_tool_errors(client, monkeypatch):
    def down():
        raise SlurmError("scontrol timed out")

    monkeypatch.setattr(deps, "show_nodes", down)
    r = await call("list_nodes")
    assert r.is_error
    assert "slurm_unreachable: scontrol timed out" in r.content[0].text

    def nss_down(username):
        raise deps.DirectoryError("sssd down")

    monkeypatch.setattr(deps, "user_groups", nss_down)
    monkeypatch.setattr(deps, "show_nodes", lambda: [])
    r = await call("list_groups")
    assert r.is_error and "directory_unreachable" in r.content[0].text


@pytest.mark.anyio
async def test_arguments_are_validated_before_the_tool_runs(client):
    assert (await call("list_jobs", {"limit": 0})).is_error
    assert (await call("list_users", {"since_hours": 721})).is_error
    assert (await call("node_detail", {"name": "gpu1", "view": "2"})).is_error


@pytest.mark.anyio
async def test_partition_queue_caps_waiting_jobs(client, monkeypatch):
    monkeypatch.setattr(deps, "queue_pending", lambda: [
        {"jobid": str(100 + i), "user": "eve", "partition": "gpu-h200",
         "state": "PENDING", "submit": "2026-08-30T12:26:40", "start": "",
         "reason": "(Resources)", "nodes": 1, "gpus": 1, "gpu_type": "h200"}
        for i in range(3)])
    http = client.get("/api/partitions/queue").json()
    assert len(http["waiting_jobs"]) == 3
    body = (await call("partition_queue", {"limit": 2})).structured_content
    assert body["waiting_jobs_total"] == 3
    assert [j["jobid"] for j in body["waiting_jobs"]] == [
        j["jobid"] for j in http["waiting_jobs"][:2]]


# ---- /mcp/ over HTTP ---------------------------------------------------

HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-06-18"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_mcp_is_disabled_without_a_token(client, monkeypatch):
    monkeypatch.delenv("MCP_TOKEN", raising=False)
    r = client.post("/mcp/", json=LIST, headers=HEADERS)
    assert r.status_code == 503
    assert r.json()["error"] == "mcp_disabled"


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic secret",
                                  "secret"])
def test_mcp_rejects_a_missing_or_wrong_token(client, monkeypatch, auth):
    monkeypatch.setenv("MCP_TOKEN", "secret")
    headers = dict(HEADERS, **({"Authorization": auth} if auth else {}))
    r = client.post("/mcp/", json=LIST, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_mcp_token_leaves_the_dashboard_open(client, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "secret")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/nope").status_code == 404  # still FastAPI's


@pytest.fixture(scope="session")
def mcp_http():
    """The app with its lifespan running (the MCP session manager).

    Session-scoped: the SDK's session manager runs once per instance, so
    the whole test session shares one started app.
    """
    with TestClient(appmod.app) as c:
        yield c


def test_tool_call_over_streamable_http(fake_prom, mcp_http, monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "secret")
    headers = dict(HEADERS, Authorization="Bearer secret")
    r = mcp_http.post("/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "cluster_status", "arguments": {}}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["cpu"]["total"] > 0
    # the trailing slash is canonical; /mcp redirects to it
    assert mcp_http.post("/mcp", json=LIST, headers=headers,
                         follow_redirects=False).status_code == 307


@pytest.mark.anyio
async def test_list_users_carries_fairshare(client):
    body = (await call("list_users")).structured_content
    assert body["fairshare_available"] is True
    by_user = {u["user"]: u for u in body["users"]}
    assert by_user["alice"]["fairshare"] == 0.25
    assert by_user["alice"]["fairshare_account"] == "aalto_users"


@pytest.mark.anyio
async def test_user_priority_matches_the_http_route(client):
    r = await call("user_priority", {"user": "alice"})
    assert not r.is_error
    assert r.structured_content == \
        client.get("/api/users/alice/priority").json()
