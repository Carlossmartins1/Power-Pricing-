"""Rutas REST v1: salud, cotizadores de electricidad y gas, back-test de cartera e inteligencia de mercado.

El estado (`MarketStore`) se construye una sola vez en el arranque de la aplicación: lee los Parquet de la
Fase 1, instancia los motores y precalcula las métricas de mercado, de modo que cada petición trabaja en memoria.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.backtester import PortfolioBacktester
from app.core.config import Settings
from app.core.pricing_engine import GasPricingEngine, PowerPricingEngine, build_load_profile, from_cents, to_cents
from app.core.regulated import GAS_REGULATORY_REFERENCE, POWER_REGULATORY_REFERENCE, regulatory_register
from app.models.portfolio import BacktestRequest, BacktestResponse
from app.models.tariffs import GasQuoteRequest, GasQuoteResponse, LoadProfileType, PowerQuoteRequest, PowerQuoteResponse
from app.pipelines.market_ingestion import (
    GAS_FILENAME,
    MANIFEST_FILENAME,
    POWER_FILENAME,
    MarketIngestionPipeline,
    load_gas_market,
    load_power_market,
)

logger = logging.getLogger("met.api")
API_VERSION = "1.0.0"

# --------------------------------------------------------------------------------------------------
# Esquemas de respuesta propios de la API
# --------------------------------------------------------------------------------------------------


class DatasetHealth(BaseModel):
    rows: int
    source: str
    sha256: str | None = None
    quality_passed: bool | None = None
    fallback_reason: str | None = None


class HealthResponse(BaseModel):
    status: str
    api_version: str
    loaded_at_utc: str
    market_year: int
    manifest_found: bool
    pipeline_version: str | None = None
    generated_at_utc: str | None = None
    cross_commodity_correlation: float | None = None
    power: DatasetHealth
    gas: DatasetHealth


class RegulatoryParameterOut(BaseModel):
    key: str
    commodity: str
    description: str
    value: str
    unit: str
    status: str
    source: str
    env_var: str | None = None
    note: str | None = None


class RegulatoryParametersResponse(BaseModel):
    """Registro de procedencia: qué valores del motor son BOE/CNMC y cuáles son hipótesis configurables."""

    power_reference: str
    gas_reference: str
    status_legend: dict[str, str]
    parameters: list[RegulatoryParameterOut]


class HourlyMarketPoint(BaseModel):
    hour: int
    spot_mean: float
    spot_winter: float
    spot_spring: float
    spot_summer: float
    spot_autumn: float
    short_premium_mean: float
    short_premium_p90: float
    long_discount_mean: float
    long_discount_p90: float
    zero_price_share_pct: float


class MonthlyMarketPoint(BaseModel):
    month: int
    baseload_eur_mwh: float
    pvb_eur_mwh: float
    ancillary_total_eur_mwh: float
    ancillary_constraints_eur_mwh: float
    ancillary_reserves_eur_mwh: float
    ancillary_other_eur_mwh: float
    zero_or_negative_hours: int


class CorrelationPoint(BaseModel):
    day: str
    season: str
    baseload_eur_mwh: float
    pvb_eur_mwh: float


class MarketSummary(BaseModel):
    baseload_eur_mwh: float
    solar_hours_price_eur_mwh: float
    evening_peak_price_eur_mwh: float
    duck_curve_spread_eur_mwh: float
    zero_or_negative_hours: int
    short_premium_mean_eur_mwh: float
    long_discount_mean_eur_mwh: float
    ancillary_mean_eur_mwh: float
    pvb_mean_eur_mwh: float
    pvb_winter_summer_spread_eur_mwh: float
    power_gas_correlation: float
    power_gas_correlation_winter: float
    power_gas_correlation_summer: float


class ArchetypeShapePoint(BaseModel):
    """Precio OMIE capturado por un arquetipo de carga y su Shape Cost frente al baseload."""

    profile: str
    captured_price_eur_mwh: float
    shape_cost_eur_mwh: float
    shape_factor: float
    solar_window_share_pct: float  # % del consumo de red entre las 10:00 y las 17:00


class MarketIntelligenceResponse(BaseModel):
    market_year: int
    data_source: str
    summary: MarketSummary
    hourly: list[HourlyMarketPoint]
    monthly: list[MonthlyMarketPoint]
    correlation: list[CorrelationPoint]
    archetype_shapes: list[ArchetypeShapePoint]


# --------------------------------------------------------------------------------------------------
# Inteligencia de mercado
# --------------------------------------------------------------------------------------------------

SEASON_BY_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
                   6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}  # fmt: skip


def archetype_shape_costs(power: pd.DataFrame) -> list[ArchetypeShapePoint]:
    """Caso de estudio de la curva de pato: qué arquetipo compra por encima o por debajo del baseload.

    Usa las mismas curvas y el mismo redondeo que el cotizador, así que el Shape Cost coincide al céntimo con el
    de `POST /price-quote/power` para cada perfil.
    """
    spot = power["spot_price_eur_mwh"].to_numpy(dtype="float64")
    hour = power["hour_local"].to_numpy(dtype=np.int16)
    baseload = float(spot.mean())
    solar_window = (hour >= 10) & (hour < 17)
    points: list[ArchetypeShapePoint] = []
    for profile in LoadProfileType:
        if profile is LoadProfileType.CUSTOM:
            continue
        load = build_load_profile(power, profile, 1.0)
        captured = float(spot @ load)
        points.append(
            ArchetypeShapePoint(
                profile=profile.value,
                captured_price_eur_mwh=round(captured, 2),
                shape_cost_eur_mwh=from_cents(to_cents(float((spot - baseload) @ load))),
                shape_factor=round(captured / baseload, 4) if baseload else 0.0,
                solar_window_share_pct=round(100.0 * float(load[solar_window].sum()), 1),
            )
        )
    return points


def build_market_intelligence(power: pd.DataFrame, gas: pd.DataFrame, market_year: int) -> MarketIntelligenceResponse:
    """Métricas agregadas del Data Warehouse: curva de pato, asimetría de desvíos, SSAA y relación gas-electricidad."""
    frame = power.assign(
        season=power["month"].map(SEASON_BY_MONTH),
        short_premium=power["imbalance_price_down_eur_mwh"] - power["spot_price_eur_mwh"],
        long_discount=power["spot_price_eur_mwh"] - power["imbalance_price_up_eur_mwh"],
        zero_price=power["spot_price_eur_mwh"] <= 0,
    )
    for optional in ("ancillary_constraints_eur_mwh", "ancillary_reserves_eur_mwh", "ancillary_other_eur_mwh"):
        if optional not in frame.columns:
            frame[optional] = 0.0

    by_hour = frame.groupby("hour_local")
    seasonal = frame.pivot_table(index="hour_local", columns="season", values="spot_price_eur_mwh", aggfunc="mean")
    hourly_stats = pd.DataFrame(
        {
            "spot_mean": by_hour["spot_price_eur_mwh"].mean(),
            "short_premium_mean": by_hour["short_premium"].mean(),
            "short_premium_p90": by_hour["short_premium"].quantile(0.9),
            "long_discount_mean": by_hour["long_discount"].mean(),
            "long_discount_p90": by_hour["long_discount"].quantile(0.9),
            "zero_price_share_pct": by_hour["zero_price"].mean() * 100.0,
        }
    )
    hourly = [
        HourlyMarketPoint(
            hour=int(hour),
            **{name: round(float(value), 2) for name, value in row.items()},
            **{f"spot_{s}": round(float(seasonal.at[hour, s]), 2) if s in seasonal.columns else 0.0 for s in ("winter", "spring", "summer", "autumn")},
        )
        for hour, row in hourly_stats.iterrows()
    ]

    gas_by_month = gas.groupby("month")["pvb_da_price_eur_mwh"].mean()
    by_month = frame.groupby("month")
    monthly = [
        MonthlyMarketPoint(
            month=int(month),
            baseload_eur_mwh=round(float(group["spot_price_eur_mwh"].mean()), 2),
            pvb_eur_mwh=round(float(gas_by_month.get(month, np.nan)), 2),
            ancillary_total_eur_mwh=round(float(group["ancillary_services_cost_eur_mwh"].mean()), 2),
            ancillary_constraints_eur_mwh=round(float(group["ancillary_constraints_eur_mwh"].mean()), 2),
            ancillary_reserves_eur_mwh=round(float(group["ancillary_reserves_eur_mwh"].mean()), 2),
            ancillary_other_eur_mwh=round(float(group["ancillary_other_eur_mwh"].mean()), 2),
            zero_or_negative_hours=int(group["zero_price"].sum()),
        )
        for month, group in by_month
    ]

    daily = (
        frame.groupby("date_local")["spot_price_eur_mwh"]
        .mean()
        .rename("baseload")
        .to_frame()
        .join(gas.set_index("gas_day")[["pvb_da_price_eur_mwh", "gas_season"]], how="inner")
    )

    def pearson(subset: pd.DataFrame) -> float:
        return round(float(subset["baseload"].corr(subset["pvb_da_price_eur_mwh"])), 4) if len(subset) > 2 else 0.0

    winter = daily[daily["gas_season"] == "WINTER"]
    summer = daily[daily["gas_season"] == "SUMMER"]
    spot_by_hour = hourly_stats["spot_mean"]
    solar, evening = float(spot_by_hour.loc[12:16].mean()), float(spot_by_hour.loc[19:22].mean())
    summary = MarketSummary(
        baseload_eur_mwh=round(float(frame["spot_price_eur_mwh"].mean()), 2),
        solar_hours_price_eur_mwh=round(solar, 2),
        evening_peak_price_eur_mwh=round(evening, 2),
        duck_curve_spread_eur_mwh=round(evening - solar, 2),
        zero_or_negative_hours=int(frame["zero_price"].sum()),
        short_premium_mean_eur_mwh=round(float(frame["short_premium"].mean()), 2),
        long_discount_mean_eur_mwh=round(float(frame["long_discount"].mean()), 2),
        ancillary_mean_eur_mwh=round(float(frame["ancillary_services_cost_eur_mwh"].mean()), 2),
        pvb_mean_eur_mwh=round(float(gas["pvb_da_price_eur_mwh"].mean()), 2),
        pvb_winter_summer_spread_eur_mwh=round(
            float(winter["pvb_da_price_eur_mwh"].mean() - summer["pvb_da_price_eur_mwh"].mean()), 2
        ),
        power_gas_correlation=pearson(daily),
        power_gas_correlation_winter=pearson(winter),
        power_gas_correlation_summer=pearson(summer),
    )
    correlation = [
        CorrelationPoint(
            day=str(day),
            season=str(row["gas_season"]),
            baseload_eur_mwh=round(float(row["baseload"]), 2),
            pvb_eur_mwh=round(float(row["pvb_da_price_eur_mwh"]), 2),
        )
        for day, row in daily.iterrows()
    ]
    data_source = str(power["data_source"].iloc[0]) if "data_source" in power.columns else "UNKNOWN"
    return MarketIntelligenceResponse(
        market_year=market_year,
        data_source=data_source,
        summary=summary,
        hourly=hourly,
        monthly=monthly,
        correlation=correlation,
        archetype_shapes=archetype_shape_costs(power),
    )


# --------------------------------------------------------------------------------------------------
# Estado en memoria
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class MarketStore:
    """Datasets, motores y agregados cargados una única vez en el arranque."""

    power: pd.DataFrame
    gas: pd.DataFrame
    power_engine: PowerPricingEngine
    gas_engine: GasPricingEngine
    backtester: PortfolioBacktester
    intelligence: MarketIntelligenceResponse
    manifest: dict[str, Any] | None
    loaded_at: datetime

    @classmethod
    def load(cls, settings: Settings) -> MarketStore:
        warehouse = settings.warehouse_dir
        if not ((warehouse / POWER_FILENAME).exists() and (warehouse / GAS_FILENAME).exists()):
            logger.warning("Data Warehouse vacío en %s: ejecutando la ingesta de la Fase 1", warehouse)
            MarketIngestionPipeline(settings).run()
        power, gas = load_power_market(warehouse), load_gas_market(warehouse)
        manifest_path = warehouse / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
        power_engine = PowerPricingEngine(power)
        store = cls(
            power=power,
            gas=gas,
            power_engine=power_engine,
            gas_engine=GasPricingEngine(gas),
            backtester=PortfolioBacktester(power, gas),
            intelligence=build_market_intelligence(power, gas, power_engine.market_year),
            manifest=manifest,
            loaded_at=datetime.now(timezone.utc),
        )
        logger.info("MarketStore cargado: %d horas de electricidad, %d días de gas", len(power), len(gas))
        return store


def get_store(request: Request) -> MarketStore:
    store: MarketStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise RuntimeError("El Data Warehouse todavía no está cargado")
    return store


# --------------------------------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1")


def _dataset_health(frame: pd.DataFrame, manifest: dict[str, Any] | None, key: str) -> DatasetHealth:
    lineage = (manifest or {}).get(key, {})
    return DatasetHealth(
        rows=len(frame),
        source=str(frame["data_source"].iloc[0]) if "data_source" in frame.columns else "UNKNOWN",
        sha256=lineage.get("sha256"),
        quality_passed=lineage.get("quality", {}).get("passed"),
        fallback_reason=lineage.get("fallback_reason"),
    )


@router.get("/health", response_model=HealthResponse, tags=["sistema"])
def health(store: MarketStore = Depends(get_store)) -> HealthResponse:
    manifest = store.manifest
    return HealthResponse(
        status="ok",
        api_version=API_VERSION,
        loaded_at_utc=store.loaded_at.isoformat(timespec="seconds"),
        market_year=store.power_engine.market_year,
        manifest_found=manifest is not None,
        pipeline_version=(manifest or {}).get("pipeline_version"),
        generated_at_utc=(manifest or {}).get("generated_at_utc"),
        cross_commodity_correlation=(manifest or {}).get("cross_commodity_correlation"),
        power=_dataset_health(store.power, manifest, "power"),
        gas=_dataset_health(store.gas, manifest, "gas"),
    )


@router.post("/price-quote/power", response_model=PowerQuoteResponse, tags=["pricing"])
def quote_power(payload: PowerQuoteRequest, store: MarketStore = Depends(get_store)) -> PowerQuoteResponse:
    return store.power_engine.quote(payload)


@router.post("/price-quote/gas", response_model=GasQuoteResponse, tags=["pricing"])
def quote_gas(payload: GasQuoteRequest, store: MarketStore = Depends(get_store)) -> GasQuoteResponse:
    return store.gas_engine.quote(payload)


@router.post("/portfolio/backtest", response_model=BacktestResponse, tags=["cartera"])
def backtest_portfolio(payload: BacktestRequest, store: MarketStore = Depends(get_store)) -> BacktestResponse:
    return store.backtester.run(payload)


@router.get("/regulatory/parameters", response_model=RegulatoryParametersResponse, tags=["sistema"])
def regulatory_parameters() -> RegulatoryParametersResponse:
    return RegulatoryParametersResponse(
        power_reference=POWER_REGULATORY_REFERENCE,
        gas_reference=GAS_REGULATORY_REFERENCE,
        status_legend={
            "OFICIAL": "Valor publicado en BOE/CNMC, copiado de la norma citada.",
            "REFERENCIA_OFICIAL": "Valor oficial usado como aproximación; se puede sobrescribir por entorno.",
            "CONFIGURABLE": "Hipótesis del comercializador; se fija por variable de entorno.",
        },
        parameters=[RegulatoryParameterOut(**row) for row in regulatory_register()],
    )


@router.get("/market/intelligence", response_model=MarketIntelligenceResponse, tags=["mercado"])
def market_intelligence(store: MarketStore = Depends(get_store)) -> MarketIntelligenceResponse:
    return store.intelligence
