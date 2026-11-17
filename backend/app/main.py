"""Aplicación FastAPI del Enterprise Multi-Commodity Power & Gas Pricing & Imbalance Engine (Fase 4).

Arranque (desde `backend/`):
    uvicorn app.main:app --reload --port 8000

Documentación interactiva en http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import API_VERSION, MarketStore, router
from app.core.config import Settings, load_settings
from app.core.pricing_engine import PricingError
from app.pipelines.market_ingestion import DataValidationError, IngestionError

logger = logging.getLogger("met.api")
# El dashboard en local se abre indistintamente como localhost o 127.0.0.1: para el navegador son orígenes distintos.
DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factoría de la aplicación. `settings` permite inyectar un warehouse aislado (tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = MarketStore.load(settings or load_settings())
        yield
        app.state.store = None

    app = FastAPI(
        title="MET Multi-Commodity Pricing & Imbalance Engine",
        description="Cotización de electricidad y gas, back-testing de cartera y atribución de P&L para el mercado ibérico.",
        version=API_VERSION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(PricingError)
    async def pricing_error_handler(_: Request, exc: PricingError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc), "error_type": "pricing_error"})

    @app.exception_handler(IngestionError)
    @app.exception_handler(DataValidationError)
    async def warehouse_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Data Warehouse no disponible: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc), "error_type": "warehouse_error"})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Error no controlado en %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Error interno del motor de pricing", "error_type": type(exc).__name__},
        )

    app.include_router(router)
    return app


app = create_app()
