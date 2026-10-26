"""GPU efficiency admin dashboard — FastAPI app.

Data is collected on demand: each route queries only the Prometheus /
sacct / scontrol sources it needs, with short in-memory TTL caches.

This module is assembly only: create the app, register the exception
handlers and routers, mount static files and the MCP endpoint. Every
route and every piece of domain logic lives in api/ and domain/
respectively.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import (
    groups,
    health,
    jobs,
    mcp_server,
    nodes,
    partitions,
    spa,
    status,
    users,
)
from deps import DirectoryError
from prom import PrometheusError
from slurm import SlurmError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# Built before the lifespan runs: the session manager it enters is
# created by build_app().
mcp_app = mcp_server.build_app()


@asynccontextmanager
async def lifespan(app):
    # A mounted app's own lifespan never runs, so the host starts the MCP
    # session manager (it owns every MCP request's task group).
    async with mcp_server.mcp.session_manager.run():
        yield


app = FastAPI(title="GPU Efficiency Dashboard", lifespan=lifespan)


@app.exception_handler(PrometheusError)
def prom_error_handler(request, exc):
    return JSONResponse(content={"error": "prometheus_unreachable",
                                  "detail": str(exc)}, status_code=502)


@app.exception_handler(SlurmError)
def slurm_error_handler(request, exc):
    return JSONResponse(content={"error": "slurm_unreachable",
                                  "detail": str(exc)}, status_code=502)


@app.exception_handler(DirectoryError)
def directory_error_handler(request, exc):
    # NSS down (no user resolvable) or prof_groups.conf unreadable: the
    # Groups tab's directory data is unavailable — 502, never a silent
    # all-unaffiliated table.
    return JSONResponse(content={"error": "directory_unreachable",
                                 "detail": str(exc)}, status_code=502)


app.include_router(health.router)
app.include_router(status.router)
app.include_router(jobs.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(partitions.router)
app.include_router(nodes.router)
app.include_router(spa.router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
# MCP (Streamable HTTP) for AI agents at /mcp/, bearer-token protected.
app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
