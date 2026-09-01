"""SPA shell routes: every deep-link path serves the same index.html and
the frontend (app.js) restores state from the URL.
"""

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(_HERE, "static")

router = APIRouter()


@router.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


# Deep-link routes: /jobs, /partitions, /users, /nodes for the plain tabs,
# plus one detail route per entity. Kept as an explicit path list rather
# than a "/{full_path:path}" catch-all so a mistyped /api/* path still
# 404s instead of silently returning HTML.
_VIEW_PATHS = [
    "/jobs", "/partitions", "/users", "/nodes",
    "/job/{jobid}", "/node/{nodename}",
    "/partition/{partition}", "/user/{username}",
]

for _p in _VIEW_PATHS:
    router.add_api_route(
        _p,
        lambda: FileResponse(os.path.join(STATIC, "index.html")),
        include_in_schema=False,
    )
