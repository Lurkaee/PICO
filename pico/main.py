from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from pico.api.chat import router as chat_router
from pico.api.conversations import router as conversations_router
from pico.api.health import router as health_router
from pico.api.homeassistant import router as homeassistant_router
from pico.api.memory import router as memory_router
from pico.api.operator import router as operator_router
from pico.api.perception import router as perception_router
from pico.api.projects import router as projects_router
from pico.api.filesystem import router as filesystem_router
from pico.api.scheduler import router as scheduler_router
from pico.api.tasks import router as tasks_router
from pico.api.tools import router as tools_router
from pico.api.workflows import router as workflows_router

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ConflictError, EntityNotFoundError, PicoError, ValidationError
from pico.db.base import Base
from pico.db.session import SessionLocal, engine
from pico.scheduler import (
    ContextBudgetExceededError,
    ModelResidencyConflictError,
    QueueFullError,
    RequestCancelledError,
    ResourceScheduler,
    SchedulingTimeoutError,
    get_scheduler,
)
from pico.providers.errors import (
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderModelNotFoundError,
    ProviderTimeoutError,
)
from pico.services.execution_engine import ToolExecutionEngine
from pico.workflow.service import WorkflowService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Ensure tables exist on startup
    Base.metadata.create_all(bind=engine)
    # Recover any interrupted executions from prior crash
    with SessionLocal() as db:
        engine_svc = ToolExecutionEngine(db)
        engine_svc.recover_interrupted_executions()
        workflow_svc = WorkflowService(db)
        workflow_svc.recover_interrupted_workflows()
    # Initialize resource scheduler
    app.state.scheduler = get_scheduler()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="PICO: Personal Intelligence Coordinator & Operator",
    lifespan=lifespan,
)

# Cross-Origin Resource Sharing (Loopback / Local Desktop Web Experience)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# Exception Handlers
@app.exception_handler(EntityNotFoundError)
async def entity_not_found_handler(
    request: Request, exc: EntityNotFoundError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": exc.message, "details": exc.details},
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.message, "details": exc.details},
    )


@app.exception_handler(AuthorizationError)
async def authorization_error_handler(
    request: Request, exc: AuthorizationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": exc.message, "details": exc.details},
    )


@app.exception_handler(ConflictError)
async def conflict_error_handler(
    request: Request, exc: ConflictError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": exc.message},
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(
    request: Request, exc: IntegrityError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": "Database integrity constraint violated (uniqueness or foreign key constraint)."
        },
    )


@app.exception_handler(ProviderConfigurationError)
async def provider_config_error_handler(
    request: Request, exc: ProviderConfigurationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(ProviderConnectionError)
async def provider_connection_error_handler(
    request: Request, exc: ProviderConnectionError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(ProviderTimeoutError)
async def provider_timeout_error_handler(
    request: Request, exc: ProviderTimeoutError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(ProviderModelNotFoundError)
async def provider_model_not_found_handler(
    request: Request, exc: ProviderModelNotFoundError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(ProviderError)
async def provider_error_handler(
    request: Request, exc: ProviderError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(QueueFullError)
async def queue_full_error_handler(request: Request, exc: QueueFullError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(SchedulingTimeoutError)
async def scheduling_timeout_error_handler(request: Request, exc: SchedulingTimeoutError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(ModelResidencyConflictError)
async def model_residency_conflict_error_handler(request: Request, exc: ModelResidencyConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(ContextBudgetExceededError)
async def context_budget_exceeded_error_handler(request: Request, exc: ContextBudgetExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(RequestCancelledError)
async def request_cancelled_error_handler(request: Request, exc: RequestCancelledError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


@app.exception_handler(PicoError)
async def pico_error_handler(request: Request, exc: PicoError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": exc.message, "details": exc.details},
    )


# Include API Routers
app.include_router(health_router)
app.include_router(operator_router)
app.include_router(conversations_router)
app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(perception_router)
app.include_router(homeassistant_router)
app.include_router(filesystem_router)
app.include_router(workflows_router)
app.include_router(scheduler_router)

# Static Frontend Production Serving (When built into frontend/dist)
frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if (frontend_dist / "assets").exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")


@app.middleware("http")
async def spa_fallback_middleware(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 404 and request.method in ("GET", "HEAD"):
        path = request.url.path.lstrip("/")
        if not (path.startswith("api") or path in ("health", "docs", "redoc", "openapi.json")):
            target_file = frontend_dist / path
            if target_file.is_file():
                from starlette.responses import FileResponse

                return FileResponse(str(target_file))
            index_file = frontend_dist / "index.html"
            if index_file.is_file():
                from starlette.responses import FileResponse

                return FileResponse(str(index_file))
    return response
