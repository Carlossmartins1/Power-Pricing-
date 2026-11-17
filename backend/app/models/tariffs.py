"""Modelos de dominio del motor de tarificación (entrada/salida de cotizaciones de electricidad y gas).

Todos los importes unitarios se expresan en €/MWh en contador y se publican redondeados al céntimo:
la suma de `build_up` coincide exactamente con `final_price_eur_mwh`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------------------------------
# Enumeraciones
# --------------------------------------------------------------------------------------------------


class PowerTariff(str, Enum):
    TD_20 = "2.0TD"
    TD_30 = "3.0TD"
    TD_61 = "6.1TD"


class LoadProfileType(str, Enum):
    INDUSTRIAL_FLAT = "INDUSTRIAL_FLAT"
    COMMERCIAL_DAY = "COMMERCIAL_DAY"
    RESIDENTIAL_EVENING = "RESIDENTIAL_EVENING"
    SOLAR_PROSUMER = "SOLAR_PROSUMER"
    SOLAR_ALIGNED_INDUSTRIAL = "SOLAR_ALIGNED_INDUSTRIAL"
    CUSTOM = "CUSTOM"


class ProductStructure(str, Enum):
    FIXED_SINGLE = "FIXED_SINGLE"
    FIXED_TOU = "FIXED_TOU"
    INDEXED = "INDEXED"
    FIXED_WITH_CAP = "FIXED_WITH_CAP"


class ImbalanceMethod(str, Enum):
    PARAMETRIC = "PARAMETRIC"
    MONTE_CARLO = "MONTE_CARLO"


class GasTariff(str, Enum):
    RL_1 = "RL.1"
    RL_2 = "RL.2"
    RL_3 = "RL.3"
    RL_4 = "RL.4"


class GasProfileType(str, Enum):
    RESIDENTIAL_HEATING = "RESIDENTIAL_HEATING"
    COMMERCIAL = "COMMERCIAL"
    INDUSTRIAL_PROCESS = "INDUSTRIAL_PROCESS"


class GasProduct(str, Enum):
    FIXED = "FIXED"
    INDEXED = "INDEXED"


class ComponentCategory(str, Enum):
    ENERGY = "ENERGY"
    BALANCING = "BALANCING"
    RISK = "RISK"
    REGULATED = "REGULATED"
    MARGIN = "MARGIN"


# --------------------------------------------------------------------------------------------------
# Estructuras comunes
# --------------------------------------------------------------------------------------------------


class BuildUpComponent(BaseModel):
    """Línea del desglose de tarifa. `pass_through=True` indica coste repercutido a mercado (no cerrado)."""

    code: str
    label: str
    category: ComponentCategory
    value_eur_mwh: float
    pass_through: bool = False


# --------------------------------------------------------------------------------------------------
# Electricidad
# --------------------------------------------------------------------------------------------------


class PowerQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tariff: PowerTariff = PowerTariff.TD_30
    profile: LoadProfileType = LoadProfileType.COMMERCIAL_DAY
    product: ProductStructure = ProductStructure.FIXED_SINGLE
    annual_consumption_mwh: float = Field(default=500.0, gt=0, le=2_000_000)
    custom_profile: list[float] | None = Field(
        default=None, max_length=8784, description="Curva horaria propia (un valor por hora del año de mercado, 8.760 en año no bisiesto)"
    )
    forecast_error_pct: float = Field(default=5.0, ge=0, le=50, description="Desviación típica del error de previsión horario (%)")
    imbalance_method: ImbalanceMethod = ImbalanceMethod.PARAMETRIC
    monte_carlo_paths: int = Field(default=200, ge=20, le=500)
    random_seed: int = 42
    risk_aversion: float = Field(default=0.10, ge=0, le=1, description="Fracción de la dispersión P95-P50 cargada como prima")
    margin_eur_mwh: float = Field(default=4.0, ge=-20, le=100)
    cap_strike_eur_mwh: float = Field(default=90.0, gt=0, le=1000)
    cap_risk_loading_pct: float = Field(default=10.0, ge=0, le=100, description="Recargo de riesgo sobre el valor histórico de la opción")
    contracted_power_kw: float | None = Field(default=None, gt=0, le=500_000)

    @model_validator(mode="after")
    def _check_custom_profile(self) -> PowerQuoteRequest:
        if self.profile is LoadProfileType.CUSTOM and not self.custom_profile:
            raise ValueError("El perfil CUSTOM requiere `custom_profile` con un valor por hora del año")
        return self


class PeriodPrice(BaseModel):
    period: str
    hours: int
    consumption_mwh: float
    consumption_share_pct: float
    omie_weighted_eur_mwh: float
    price_eur_mwh: float
    components: dict[str, float]


class HourlyProfilePoint(BaseModel):
    hour: int
    client_load_mw: float
    client_load_index: float
    omie_price_eur_mwh: float
    omie_price_index: float


class PowerQuoteKpis(BaseModel):
    final_price_eur_mwh: float
    baseload_omie_eur_mwh: float
    weighted_omie_eur_mwh: float
    shape_cost_eur_mwh: float
    shape_factor: float
    ancillary_cost_eur_mwh: float
    imbalance_cost_eur_mwh: float
    imbalance_cost_p95_eur_mwh: float | None = None
    risk_premium_eur_mwh: float
    regulated_cost_eur_mwh: float
    margin_eur_mwh: float
    cap_strike_eur_mwh: float | None = None
    cap_option_premium_eur_mwh: float | None = None
    cap_hours_in_the_money: int | None = None
    annual_energy_bill_eur: float
    expected_gross_margin_eur: float
    power_term_eur_year: float | None = None


class PowerQuoteResponse(BaseModel):
    tariff: PowerTariff
    profile: LoadProfileType
    product: ProductStructure
    annual_consumption_mwh: float
    market_year: int
    data_source: str
    kpis: PowerQuoteKpis
    build_up: list[BuildUpComponent]
    period_prices: list[PeriodPrice]
    tou_weighted_average_eur_mwh: float
    tou_reconciliation_diff_eur_mwh: float
    hourly_profile: list[HourlyProfilePoint]
    regulatory_reference: str
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------------
# Gas natural
# --------------------------------------------------------------------------------------------------


class GasQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tariff: GasTariff = GasTariff.RL_2
    profile: GasProfileType = GasProfileType.RESIDENTIAL_HEATING
    product: GasProduct = GasProduct.FIXED
    annual_consumption_mwh: float = Field(default=9.0, gt=0, le=100_000)
    forecast_error_pct: float = Field(default=8.0, ge=0, le=50, description="Desviación típica del error de nominación diaria (%)")
    risk_aversion: float = Field(default=0.10, ge=0, le=1)
    margin_eur_mwh: float = Field(default=3.0, ge=-20, le=100)


class GasMonthlyPoint(BaseModel):
    month: int
    consumption_mwh: float
    consumption_share_pct: float
    pvb_avg_eur_mwh: float
    hdd: float


class GasQuoteKpis(BaseModel):
    final_price_eur_mwh: float
    baseload_pvb_eur_mwh: float
    weighted_pvb_eur_mwh: float
    seasonal_shape_cost_eur_mwh: float
    shape_factor: float
    imbalance_cost_eur_mwh: float
    risk_premium_eur_mwh: float
    regulated_cost_eur_mwh: float
    margin_eur_mwh: float
    winter_consumption_share_pct: float
    annual_bill_eur: float
    expected_gross_margin_eur: float


class GasQuoteResponse(BaseModel):
    tariff: GasTariff
    profile: GasProfileType
    product: GasProduct
    annual_consumption_mwh: float
    market_year: int
    data_source: str
    kpis: GasQuoteKpis
    build_up: list[BuildUpComponent]
    monthly: list[GasMonthlyPoint]
    regulatory_reference: str
    warnings: list[str] = Field(default_factory=list)
