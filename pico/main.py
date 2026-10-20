from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from pico.api.chat import router as chat_router
from pico.api.conversations import router as conversations_router
from pico.api.health import router as health_router
from pico.api.memory import router as memory_router
from pico.api.perception import router as perception_router
from pico.api.projects import router as projects_router
from pico.api.tasks import router as tasks_router
from pico.api.tools import router as tools_router
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ConflictError, EntityNotFoundError, PicoError, ValidationError
from pico.db.base import Base
from pico.db.session import SessionLocal, engine
from pico.providers.errors import (
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderModelNotFoundError,
    ProviderTimeoutError,
)
from pico.services.execution_engine import ToolExecutionEngine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Ensure tables exist on startup
    Base.metadata.create_all(bind=engine)
    # Recover any interrupted executions from prior crash
    with SessionLocal() as db:
        engine_svc = ToolExecutionEngine(db)
        engine_svc.recover_interrupted_executions()
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="PICO: Personal Intelligence Coordinator & Operator",
    lifespan=lifespan,
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


@app.exception_handler(PicoError)
async def pico_error_handler(request: Request, exc: PicoError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": exc.message, "details": exc.details},
    )


# Include API Routers
app.include_router(health_router)
app.include_router(conversations_router)
app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(perception_router)
