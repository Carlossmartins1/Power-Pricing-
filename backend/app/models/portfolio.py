"""Modelos del back-testing de cartera, escenarios de estrés y atribución de P&L."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Commodity(str, Enum):
    POWER = "POWER"
    GAS = "GAS"


class ContractProduct(str, Enum):
    FIXED = "FIXED"
    INDEXED = "INDEXED"


class AlertLevel(str, Enum):
    OK = "OK"
    REPRICE_REQUIRED = "REPRICE_REQUIRED"


class StressScenario(BaseModel):
    """Escenario paramétrico aplicado sobre el periodo de entrega ya transcurrido."""

    model_config = ConfigDict(extra="forbid")

    volume_shock_pct: float = Field(default=0.0, ge=-50, le=100, description="Desviación de demanda por clima (+15 = ola de calor/frío)")
    shape_shift_pct: float = Field(default=0.0, ge=-50, le=50, description="Consumo que sale de las horas solares (+) o entra en ellas (-)")
    imbalance_multiplier: float = Field(default=1.0, ge=0, le=10, description="Multiplicador de los spreads de desvío REE / Enagás")
    unhedged_ratio: float = Field(default=0.15, ge=0, le=1, description="Fracción del volumen contratado sin cobertura a plazo")
    wholesale_price_shock_pct: float = Field(default=0.0, ge=-50, le=200, description="Desplazamiento del spot OMIE / PVB realizado")


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_contracts: int = Field(default=500, ge=10, le=5000)
    seed: int = 7
    stress: StressScenario = Field(default_factory=StressScenario)
    margin_threshold_eur_mwh: float = Field(default=2.0, ge=-50, le=50)
    max_alerts: int = Field(default=25, ge=1, le=500)


class ContractResult(BaseModel):
    contract_id: str
    commodity: Commodity
    tariff: str
    profile: str
    product: ContractProduct
    signed_months_ago: int
    contracted_mwh: float
    realized_mwh: float
    contract_price_eur_mwh: float
    ex_ante_margin_eur: float
    ex_post_margin_eur: float
    volume_effect_eur: float
    shape_effect_eur: float
    wholesale_hedging_effect_eur: float
    imbalance_effect_eur: float
    ex_ante_unit_margin_eur_mwh: float
    realized_unit_margin_eur_mwh: float
    alert: AlertLevel


class WaterfallStep(BaseModel):
    key: str
    label: str
    kind: str  # "total" | "effect"
    value_eur: float
    start_eur: float
    end_eur: float


class PnlAttribution(BaseModel):
    volume_effect_eur: float
    shape_effect_eur: float
    wholesale_hedging_effect_eur: float
    imbalance_effect_eur: float


class SegmentSummary(BaseModel):
    tariff: str
    commodity: Commodity
    contracts: int
    contracted_mwh: float
    realized_mwh: float
    ex_ante_margin_eur: float
    ex_post_margin_eur: float
    realized_unit_margin_eur_mwh: float
    contracts_at_risk: int


class BacktestKpis(BaseModel):
    contracts: int
    contracted_mwh: float
    realized_mwh: float
    ex_ante_margin_eur: float
    ex_post_margin_eur: float
    margin_delta_eur: float
    margin_delta_pct: float
    ex_ante_unit_margin_eur_mwh: float
    ex_post_unit_margin_eur_mwh: float
    contracts_at_risk: int
    at_risk_volume_share_pct: float
    open_position_mwh: float


class BacktestResponse(BaseModel):
    market_year: int
    data_source: str
    stress: StressScenario
    margin_threshold_eur_mwh: float
    kpis: BacktestKpis
    attribution: PnlAttribution
    waterfall: list[WaterfallStep]
    reconciliation_error_eur: float
    segments: list[SegmentSummary]
    at_risk_contracts: list[ContractResult]
    assumptions: list[str]
