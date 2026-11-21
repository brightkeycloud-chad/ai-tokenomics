"""Service Status Board.

A small internal board showing the health of a handful of services, plus the
incident history behind each one. Read the conventions in AGENTS.md before
adding a route.
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import store
from .models import HealthResponse, Incident, Service, ServiceDetail
from .validators import check_service_id

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Service Status Board",
    description="Current health of our services, and the incidents behind it.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/healthz", response_model=HealthResponse, tags=["operations"])
def healthz() -> HealthResponse:
    """Report that the process is up.

    This check must stay free of dependencies. It answers for the process and
    nothing else, so that a failing health check means the load balancer cannot
    reach this container, and never that a downstream service is unhappy.
    """
    return HealthResponse(status="ok")


@app.get("/api/services", response_model=list[Service], tags=["services"])
def api_list_services() -> list[Service]:
    """Return every service on the board."""
    return [Service(**item) for item in store.list_services()]


@app.get(
    "/api/services/{service_id}", response_model=ServiceDetail, tags=["services"]
)
def api_get_service(service_id: str) -> ServiceDetail:
    """Return one service together with its recent incidents."""
    try:
        clean_id = check_service_id(service_id)
    except ValueError as error:
        logger.warning("rejected service_id: %s", error)
        raise HTTPException(status_code=400, detail=str(error)) from error

    item = store.get_service(clean_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"no service with id {clean_id!r}"
        )

    return ServiceDetail(
        service=Service(**item),
        incidents=[Incident(**row) for row in store.list_incidents(clean_id)],
    )


@app.get("/", response_class=HTMLResponse, tags=["board"])
def board(request: Request) -> HTMLResponse:
    """Render the status board."""
    services = store.list_services()
    return templates.TemplateResponse(
        request=request, name="board.html", context={"services": services}
    )
