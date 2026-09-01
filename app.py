"""GPU efficiency admin dashboard — FastAPI app.

Data is collected on demand: each route queries only the Prometheus /
sacct / scontrol sources it needs, with short in-memory TTL caches.

This module is assembly only: create the app, register the exception
handlers and routers, mount static files. Every route and every piece
of domain logic lives in api/ and domain/ respectively.
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import health, jobs, nodes, partitions, spa, users
from prom import PrometheusError
from slurm import SlurmError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="GPU Efficiency Dashboard")


@app.exception_handler(PrometheusError)
def prom_error_handler(request, exc):
    return JSONResponse(content={"error": "prometheus_unreachable",
                                  "detail": str(exc)}, status_code=502)


@app.exception_handler(SlurmError)
def slurm_error_handler(request, exc):
    return JSONResponse(content={"error": "slurm_unreachable",
                                 "detail": str(exc)}, status_code=502)


app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(users.router)
app.include_router(partitions.router)
app.include_router(nodes.router)
app.include_router(spa.router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
