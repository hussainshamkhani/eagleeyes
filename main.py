import os
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router
from db.mongo import mongo_client
from integrations.arize_client import setup_arize_tracing, start_local_phoenix, shutdown_tracing
from core.config import settings

logger = logging.getLogger("eagleeyes")

# Cloud Run (and other Knative runtimes) always set K_SERVICE. We use it to
# detect "running in the cloud" independently of ENVIRONMENT, so dev-only
# behavior (in-process Phoenix) never runs in a container.
ON_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup -------------------------------------------------------------
    # Do NOT let a slow/failed Mongo connect block the port from opening:
    # Cloud Run kills the container if nothing listens on $PORT within the
    # startup-probe window. Connect best-effort and log loudly on failure;
    # the app still binds and serves (DB errors then surface per-request).
    try:
        await mongo_client.connect()
    except Exception:
        logger.exception(
            "MongoDB connect failed at startup — app will start anyway; "
            "check MONGODB_URI (trailing newline?) and Atlas network access."
        )

    # Local in-process Phoenix is for local dev only. On Cloud Run we export
    # to Phoenix Cloud, so skip it entirely (it would otherwise try to bind a
    # port / spawn a server inside the container).
    if settings.ENVIRONMENT == "development" and not ON_CLOUD_RUN:
        start_local_phoenix()

    try:
        setup_arize_tracing()
    except Exception:
        logger.exception("Arize tracing setup failed — continuing without it.")

    yield

    # Shutdown ------------------------------------------------------------
    await mongo_client.disconnect()
    shutdown_tracing()


app = FastAPI(
    title="EagleEyes AML Agent",
    description="Self-Improving Fraud Detection Agent for Kuwait Exchange Houses",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Custom Request Logging Middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start_time
    logger.info(
        "Method: %s | Path: %s | Status: %d | Duration: %.4fs",
        request.method,
        request.url.path,
        response.status_code,
        duration
    )
    return response


app.include_router(router, prefix="/api/v1")

from fastapi.responses import RedirectResponse


@app.get("/")
async def redirect_to_dashboard():
    return RedirectResponse(url="/dashboard.html")

app.mount("/", StaticFiles(directory="ui", html=True), name="ui")