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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await mongo_client.connect()
    
    if settings.ENVIRONMENT == "development":
        start_local_phoenix()
    
    setup_arize_tracing()
    
    yield
    
    # Shutdown
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

