from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes.auth import router as auth_router
from app.core.config import settings
from app.schemas.health import HealthResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup — add db pool init, redis connect, etc. here as needed
    yield
    # shutdown — close connections here as needed


app = FastAPI(
    title="Matata Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # exc is guaranteed to be HTTPException by the handler registration below,
    # but the signature must accept Exception to satisfy Starlette's type contract.
    http_exc = exc if isinstance(exc, HTTPException) else HTTPException(status_code=500)
    return JSONResponse(
        status_code=http_exc.status_code,
        content={"error": http_exc.detail},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred."},
    )


app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/v1")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version="0.1.0")
