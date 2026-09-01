"""Backend + Prometheus connectivity check."""

from datetime import datetime, timezone

from fastapi import APIRouter

import deps
from api.schemas import HealthResponse

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
def health():
    return {
        "ok": True,
        "prometheus": deps.get_prom().api_base,
        "time": datetime.now(timezone.utc).isoformat(),
    }
