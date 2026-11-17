"""Motor cuantitativo de profiling y tarificación Multi-Commodity (Electricidad OMIE/REE + Gas MIBGAS/Enagás).

Fase 2 del Enterprise Multi-Commodity Power & Gas Pricing & Imbalance Engine. Consume el Data Warehouse
de la Fase 1 (`power_market.parquet`, `gas_market.parquet`) y construye la tarifa componente a componente:

    Tarifa = OMIE baseload + Shape + Pérdidas + SSAA + Desvíos + Prima de riesgo + Peajes/Cargos + Margen

Caso de estudio: el Shape Cost en un mercado con mucha solar
-----------------------------------------------------------
El Shape Cost es el precio OMIE ponderado por la curva del cliente menos el baseload. En el mercado ibérico la
fotovoltaica hunde el precio de las horas centrales y lo dispara al atardecer (curva de pato), así que lo que
decide el signo no es cuánta energía «verde» usa el cliente, sino a qué horas compra de la red:

* ``SOLAR_PROSUMER`` (autoconsumo) tiene un Shape Cost MAYOR que el residencial estándar. Sus placas cubren el
  mediodía, justo cuando la energía es barata, y lo que compra a la comercializadora es el residuo de la
  mañana y, sobre todo, de la punta de 20-22 h. Menos volumen, pero el MWh medio más caro de la cartera.
* ``SOLAR_ALIGNED_INDUSTRIAL`` (bombeo solar, desalación, frío industrial flexible) hace lo contrario: concentra
  la compra de red entre las 10 y las 17 h y captura los precios valle o cero. Su Shape Cost es NEGATIVO.

Los tests fijan ambas propiedades: shape(SOLAR_PROSUMER) > shape(RESIDENTIAL_EVENING) y
shape(SOLAR_ALIGNED_INDUSTRIAL) < 0. Cotizar al prosumidor «con descuento por ser solar» destruye margen.

Convenciones
------------
* Todo en €/MWh en contador. Cada componente se redondea al céntimo (HALF_UP) y el precio final es la suma
  exacta, en céntimos enteros, de las líneas publicadas: el desglose es auditable sin diferencias de redondeo.
* Desvíos: el cliente que consume más de lo previsto deja a la comercializadora corta (compra el déficit al
  precio de desvíos a bajar, >= spot); si consume menos queda larga (vende el excedente al precio de desvíos
  a subir, <= spot). Con error ~ N(0, sigma) el coste esperado por MWh es sigma/sqrt(2*pi) * (bajar - subir).
* Costes regulados: viven en `app.core.regulated`, que declara para cada valor su norma (BOE/CNMC) y si es
  oficial o un parámetro configurable. Los pagos por capacidad y las retribuciones de OMIE y del OS se
  publican sobre energía en barras de central: aquí se elevan a contador con el coeficiente de pérdidas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, Sequence

import numpy as np
import pandas as pd

from app.core.regulated import (
    FNEE_EUR_MWH,
    GAS_CNMC_FEE_RATIO,
    GAS_FNEE_EUR_MWH,
    GAS_GTS_QUOTA_RATIO,
    GAS_REGULATED,
    GAS_REGULATORY_REFERENCE,
    GAS_SHRINKAGE_RATIO,
    OMIE_FEE_EUR_MWH,
    OS_FEE_EUR_MWH,
    POWER_REGULATED,
    POWER_REGULATORY_REFERENCE,
    GasRegulatedTable,
    PowerRegulatedTable,
)
from app.models.tariffs import (
    BuildUpComponent,
    ComponentCategory,
    GasMonthlyPoint,
    GasProduct,
    GasProfileType,
    GasQuoteKpis,
    GasQuoteRequest,
    GasQuoteResponse,
    HourlyProfilePoint,
    ImbalanceMethod,
    LoadProfileType,
    PeriodPrice,
    PowerQuoteKpis,
    PowerQuoteRequest,
    PowerQuoteResponse,
    PowerTariff,
    ProductStructure,
)
from app.pipelines.market_ingestion import load_gas_market, load_power_market

__all__ = [
    "GAS_REGULATED",
    "GAS_REGULATORY_REFERENCE",
    "POWER_REGULATED",
    "POWER_REGULATORY_REFERENCE",
    "GasPricingEngine",
    "GasRegulatedTable",
    "PowerPricingEngine",
    "PowerRegulatedTable",
    "PricingError",
    "assign_periods",
    "build_gas_profile",
    "build_load_profile",
    "from_cents",
    "to_cents",
]

# E[max(e, 0)] para e ~ N(0, 1): fracción esperada de energía en desvío por cada lado.
HALF_NORMAL_MEAN: Final[float] = 1.0 / math.sqrt(2.0 * math.pi)
IMBALANCE_ERROR_AUTOCORRELATION: Final[float] = 0.6

POWER_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "datetime_utc",
    "datetime_local",
    "date_local",
    "hour_local",
    "month",
    "is_weekend",
    "is_tariff_holiday",
    "spot_price_eur_mwh",
    "imbalance_price_up_eur_mwh",
    "imbalance_price_down_eur_mwh",
    "ancillary_services_cost_eur_mwh",
)
GAS_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "gas_day",
    "month",
    "gas_season",
    "is_weekend",
    "pvb_da_price_eur_mwh",
    "imbalance_buy_price_eur_mwh",
    "imbalance_sell_price_eur_mwh",
    "hdd_18c",
)


class PricingError(ValueError):
    """Entrada de cotización incoherente con el Data Warehouse o con la tarifa solicitada."""


def to_cents(value: float) -> int:
    """Redondeo comercial (HALF_UP) a céntimos enteros de €/MWh.

    Se normaliza antes a 8 decimales para que el ruido binario de las medias ponderadas (3,2149999...)
    no cambie el céntimo de un valor regulado publicado (3,215 -> 3,22).
    """
    return int((Decimal(repr(round(float(value), 8))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> float:
    return cents / 100.0


MAX_ANNUAL_MWH_20TD: Final[float] = 15.0 * 8760 / 1000.0

# --------------------------------------------------------------------------------------------------
# Periodos tarifarios (Circular CNMC 3/2020, calendario peninsular)
# --------------------------------------------------------------------------------------------------

# Temporada eléctrica por mes: 0 = alta, 1 = media-alta, 2 = media, 3 = baja.
SEASON_BY_MONTH: Final[np.ndarray] = np.array([0, 0, 1, 3, 3, 2, 0, 2, 2, 3, 1, 0], dtype=np.int8)


def assign_periods(calendar: pd.DataFrame, tariff: PowerTariff) -> np.ndarray:
    """Periodo tarifario de cada hora (vectorizado). Devuelve enteros 1..3 (2.0TD) o 1..6 (3.0TD / 6.1TD).

    * 2.0TD: punta P1 (10-14 h y 18-22 h), llano P2 (8-10, 14-18, 22-24 h) y valle P3 (0-8 h).
    * 3.0TD / 6.1TD: las horas punta (9-14, 18-22 h) toman P1/P2/P3/P4 según temporada y las llanas
      (8-9, 14-18, 22-24 h) el periodo siguiente; 0-8 h es siempre P6.
    * Sábados, domingos y festivos nacionales de fecha fija son valle completo (P3 o P6).
    """
    hour = calendar["hour_local"].to_numpy(dtype=np.int16)
    month = calendar["month"].to_numpy(dtype=np.int16)
    off_peak_day = (calendar["is_weekend"].to_numpy(dtype=bool)) | (calendar["is_tariff_holiday"].to_numpy(dtype=bool))
    night = hour < 8
    if tariff is PowerTariff.TD_20:
        peak = ((hour >= 10) & (hour < 14)) | ((hour >= 18) & (hour < 22))
        periods = np.where(peak, 1, 2).astype(np.int8)
        periods[night | off_peak_day] = 3
        return periods
    peak = ((hour >= 9) & (hour < 14)) | ((hour >= 18) & (hour < 22))
    periods = (SEASON_BY_MONTH[month - 1] + np.where(peak, 1, 2)).astype(np.int8)
    periods[night | off_peak_day] = 6
    return periods


# --------------------------------------------------------------------------------------------------
# Profiling de carga
# --------------------------------------------------------------------------------------------------


def _bump(hour: np.ndarray, center: float, width: float) -> np.ndarray:
    """Campana gaussiana sobre el reloj de 24 h (distancia circular, sin salto a medianoche)."""
    distance = (hour - center + 12.0) % 24.0 - 12.0
    return np.exp(-0.5 * (distance / width) ** 2)


def _plateau(hour: np.ndarray, start: float, end: float, rise: float = 0.7, fall: float = 0.9) -> np.ndarray:
    """Meseta entre dos horas con flancos logísticos; `rise` y `fall` son la anchura (en horas) de cada rampa."""
    return 1.0 / (1.0 + np.exp(-(hour - start) / rise)) / (1.0 + np.exp((hour - end) / fall))


def build_load_profile(
    calendar: pd.DataFrame,
    profile: LoadProfileType,
    annual_mwh: float,
    custom: Sequence[float] | None = None,
) -> np.ndarray:
    """Curva horaria de consumo de red (MWh por hora) escalada al consumo anual y alineada con el calendario.

    Arquetipos (lo que se perfila es siempre la energía comprada a la red, no el consumo bruto del cliente):

    * ``INDUSTRIAL_FLAT``          Proceso continuo 24x7. Shape Cost nulo por construcción.
    * ``COMMERCIAL_DAY``           Horario comercial 8:30-19:30 con refuerzo de climatización en verano.
    * ``RESIDENTIAL_EVENING``      Hogar con punta a las 21 h y más consumo en invierno.
    * ``SOLAR_PROSUMER``           Hogar o pyme con autoconsumo: consumo bruto menos generación propia. La
                                   generación vacía el mediodía (más en verano) y la red solo suministra la mañana
                                   y la punta de tarde-noche, las horas más caras: su Shape Cost supera al del
                                   residencial estándar aunque compre menos energía.
    * ``SOLAR_ALIGNED_INDUSTRIAL`` Carga flexible que sigue al sol (bombeo solar, desalación, frío industrial):
                                   opera a plena carga de 10 a 17 h los siete días, más en verano, y fuera de esa
                                   ventana solo mantiene auxiliares. Compra en las horas de precio valle o cero:
                                   Shape Cost negativo.
    * ``CUSTOM``                   Curva propia del cliente, un valor por hora del año de mercado.
    """
    n = len(calendar)
    if profile is LoadProfileType.CUSTOM:
        if custom is None or len(custom) != n:
            received = 0 if custom is None else len(custom)
            raise PricingError(f"La curva personalizada debe tener {n} valores horarios (recibidos: {received})")
        raw = np.asarray(custom, dtype="float64")
        if not np.isfinite(raw).all() or (raw < 0).any() or raw.sum() <= 0:
            raise PricingError("La curva personalizada debe ser finita, no negativa y con consumo total positivo")
        return raw / raw.sum() * annual_mwh

    hour = calendar["hour_local"].to_numpy(dtype="float64") + 0.5
    month = calendar["month"].to_numpy(dtype="float64")
    off_day = calendar["is_weekend"].to_numpy(dtype=bool) | calendar["is_tariff_holiday"].to_numpy(dtype=bool)
    winter = 0.5 * (1.0 + np.cos(2.0 * np.pi * (month - 1.0) / 12.0))  # 1 en enero, 0 en julio
    summer = 1.0 - winter

    if profile is LoadProfileType.INDUSTRIAL_FLAT:
        raw = np.ones(n)
    elif profile is LoadProfileType.COMMERCIAL_DAY:
        open_hours = _plateau(hour, 8.5, 19.5)
        workday = 0.18 + open_hours * (1.0 + 0.25 * summer * _bump(hour, 15.0, 3.0))
        raw = np.where(off_day, 0.18 + 0.30 * open_hours, workday) * (1.0 + 0.12 * summer + 0.08 * winter)
    elif profile is LoadProfileType.RESIDENTIAL_EVENING:
        workday = 0.35 + 0.30 * _bump(hour, 8.0, 1.4) + 0.15 * _bump(hour, 14.5, 1.5) + 1.00 * _bump(hour, 21.0, 2.3)
        weekend = 0.38 + 0.25 * _bump(hour, 10.0, 2.0) + 0.25 * _bump(hour, 14.5, 1.8) + 0.95 * _bump(hour, 21.0, 2.4)
        raw = np.where(off_day, weekend, workday) * (0.85 + 0.45 * winter + 0.12 * summer**2)
    elif profile is LoadProfileType.SOLAR_PROSUMER:
        gross = 0.40 + 0.25 * _bump(hour, 8.0, 1.5) + 0.35 * _bump(hour, 13.0, 4.0) + 0.85 * _bump(hour, 21.0, 2.3)
        self_generation = 1.15 * (0.45 + 0.55 * summer) * _bump(hour, 14.0, 2.6 + 0.9 * summer)
        raw = np.maximum(gross - self_generation, 0.06 * gross) * (0.90 + 0.30 * winter)
    elif profile is LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL:
        auxiliaries = 0.06  # control, alumbrado y mantenimiento de presión/frío fuera de la ventana solar
        solar_window = _plateau(hour, 10.0, 17.0, rise=0.25, fall=0.25)  # arranque y parada en ~30 min
        raw = (auxiliaries + solar_window * (0.70 + 0.30 * summer)) * np.where(off_day, 0.92, 1.0)
    else:  # pragma: no cover - enumeración cerrada
        raise PricingError(f"Perfil de carga no soportado: {profile}")
    return raw / raw.sum() * annual_mwh


def build_gas_profile(calendar: pd.DataFrame, profile: GasProfileType, annual_mwh: float) -> np.ndarray:
    """Consumo diario de gas (MWh/día): parte base plana + parte térmica proporcional a los HDD (base 18 °C)."""
    base_share, weekend_factor = GAS_PROFILE_PARAMETERS[profile]
    hdd = calendar["hdd_18c"].to_numpy(dtype="float64")
    flat = np.where(calendar["is_weekend"].to_numpy(dtype=bool), weekend_factor, 1.0)
    flat = flat / flat.sum()
    heating = hdd / hdd.sum() if hdd.sum() > 0 else np.full(len(hdd), 1.0 / len(hdd))
    return (base_share * flat + (1.0 - base_share) * heating) * annual_mwh


# (fracción de consumo base no térmico, factor de actividad en fin de semana)
GAS_PROFILE_PARAMETERS: Final[dict[GasProfileType, tuple[float, float]]] = {
    GasProfileType.RESIDENTIAL_HEATING: (0.15, 1.00),
    GasProfileType.COMMERCIAL: (0.40, 0.70),
    GasProfileType.INDUSTRIAL_PROCESS: (0.90, 0.80),
}

# --------------------------------------------------------------------------------------------------
# Motor de electricidad
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ComponentSpec:
    code: str
    label: str
    category: ComponentCategory


_FIXED_PRODUCTS: Final[frozenset[ProductStructure]] = frozenset({ProductStructure.FIXED_SINGLE, ProductStructure.FIXED_TOU})


class PowerPricingEngine:
    """Cotizador eléctrico: perfila la carga y construye la tarifa por componentes y por periodo P1-P6."""

    def __init__(self, market: pd.DataFrame) -> None:
        missing = [c for c in POWER_REQUIRED_COLUMNS if c not in market.columns]
        if missing:
            raise PricingError(f"power_market sin columnas requeridas: {missing}")
        if market.empty:
            raise PricingError("power_market está vacío")
        self.market = market.reset_index(drop=True)
        self.spot = self.market["spot_price_eur_mwh"].to_numpy(dtype="float64")
        self.imbalance_up = self.market["imbalance_price_up_eur_mwh"].to_numpy(dtype="float64")
        self.imbalance_down = self.market["imbalance_price_down_eur_mwh"].to_numpy(dtype="float64")
        self.ancillary = self.market["ancillary_services_cost_eur_mwh"].to_numpy(dtype="float64")
        self.hour = self.market["hour_local"].to_numpy(dtype=np.int16)
        self.day_index = pd.factorize(self.market["date_local"])[0]
        self.baseload = float(self.spot.mean())
        self.market_year = int(pd.Timestamp(self.market["datetime_local"].iloc[len(self.market) // 2]).year)
        self.data_source = str(self.market["data_source"].iloc[0]) if "data_source" in self.market.columns else "UNKNOWN"
        self._periods: dict[PowerTariff, np.ndarray] = {}

    @classmethod
    def from_warehouse(cls, warehouse_dir: Path) -> PowerPricingEngine:
        return cls(load_power_market(warehouse_dir))

    def periods(self, tariff: PowerTariff) -> np.ndarray:
        if tariff not in self._periods:
            self._periods[tariff] = assign_periods(self.market, tariff)
        return self._periods[tariff]

    # ---------------- componentes ----------------

    def imbalance_unit_cost(self, load: np.ndarray, request: PowerQuoteRequest) -> tuple[np.ndarray, float | None]:
        """Coste esperado de desvíos por hora (€/MWh consumido) y, con Monte Carlo, su P95 anual."""
        sigma = request.forecast_error_pct / 100.0
        short_spread = self.imbalance_down - self.spot  # sobrecoste por MWh en posición corta
        long_spread = self.spot - self.imbalance_up  # pérdida por MWh en posición larga
        if request.imbalance_method is ImbalanceMethod.PARAMETRIC or sigma == 0.0:
            return sigma * HALF_NORMAL_MEAN * (short_spread + long_spread), None
        rng = np.random.default_rng(request.random_seed)
        n_paths, n_hours = request.monte_carlo_paths, len(load)
        phi = IMBALANCE_ERROR_AUTOCORRELATION
        shocks = rng.standard_normal((n_paths, n_hours))
        errors = np.empty_like(shocks)
        errors[:, 0] = shocks[:, 0]
        innovation = math.sqrt(1.0 - phi**2)
        for t in range(1, n_hours):
            errors[:, t] = phi * errors[:, t - 1] + innovation * shocks[:, t]
        errors *= sigma
        path_cost = np.maximum(errors, 0.0) * short_spread + np.maximum(-errors, 0.0) * long_spread
        annual_by_path = path_cost @ load / load.sum()
        return path_cost.mean(axis=0), float(np.percentile(annual_by_path, 95))

    def volatility_premium(self, load: np.ndarray, risk_aversion: float) -> float:
        """Prima por volatilidad: fracción de la dispersión P95-P50 del coste diario ponderado por el perfil."""
        daily_energy = np.bincount(self.day_index, weights=load)
        daily_cost = np.bincount(self.day_index, weights=load * self.spot)
        valid = daily_energy > 0
        daily_price = daily_cost[valid] / daily_energy[valid]
        return float(risk_aversion * (np.percentile(daily_price, 95) - np.percentile(daily_price, 50)))

    # ---------------- cotización ----------------

    def quote(self, request: PowerQuoteRequest) -> PowerQuoteResponse:
        load = build_load_profile(self.market, request.profile, request.annual_consumption_mwh, request.custom_profile)
        energy = float(load.sum())
        table = POWER_REGULATED[request.tariff]
        periods = self.periods(request.tariff)
        pidx = periods - 1
        product = request.product
        is_fixed = product in _FIXED_PRODUCTS
        is_cap = product is ProductStructure.FIXED_WITH_CAP

        imbalance_unit, imbalance_p95 = self.imbalance_unit_cost(load, request)
        loss_ratio = np.asarray(table.losses)[pidx]
        to_meter = 1.0 + loss_ratio  # de barras de central a contador
        cap_payoff = np.maximum(self.spot - request.cap_strike_eur_mwh, 0.0)
        cap_value = float(cap_payoff @ load / energy)
        cap_premium = cap_value * (1.0 + request.cap_risk_loading_pct / 100.0)
        risk_premium = self.volatility_premium(load, request.risk_aversion) if is_fixed else 0.0

        # (especificación, coste unitario horario o escalar plano)
        lines: list[tuple[_ComponentSpec, np.ndarray | float]] = [
            (_ComponentSpec("OMIE_BASELOAD", "Energía OMIE baseload", ComponentCategory.ENERGY), self.baseload),
            (_ComponentSpec("SHAPE", "Coste de perfilado (shape)", ComponentCategory.ENERGY), self.spot - self.baseload),
        ]
        if is_cap:
            label = f"Ahorro esperado por cap a {request.cap_strike_eur_mwh:.2f} €/MWh"
            lines.append((_ComponentSpec("CAP_PAYOFF", label, ComponentCategory.ENERGY), -cap_payoff))
        lines += [
            (_ComponentSpec("LOSSES", "Pérdidas de red", ComponentCategory.ENERGY), loss_ratio * (self.spot + self.ancillary)),
            (_ComponentSpec("SSAA", "Servicios de ajuste REE", ComponentCategory.BALANCING), self.ancillary),
            (_ComponentSpec("IMBALANCE", "Coste de desvíos REE", ComponentCategory.BALANCING), imbalance_unit),
        ]
        if is_fixed:
            lines.append((_ComponentSpec("RISK_PREMIUM", "Prima de riesgo por volatilidad", ComponentCategory.RISK), risk_premium))
        if is_cap:
            lines.append((_ComponentSpec("CAP_PREMIUM", "Prima de la opción cap", ComponentCategory.RISK), cap_premium))
        margin_label = "Margen comercial" if is_fixed else "Fee de gestión"
        lines += [
            (_ComponentSpec("TOLLS", "Peajes T&D (energía)", ComponentCategory.REGULATED), np.asarray(table.toll_energy)[pidx]),
            (_ComponentSpec("CHARGES", "Cargos del sistema (energía)", ComponentCategory.REGULATED), np.asarray(table.charge_energy)[pidx]),
            (
                _ComponentSpec("CAPACITY", "Pagos por capacidad", ComponentCategory.REGULATED),
                np.asarray(table.capacity_payment)[pidx] * to_meter,
            ),
            (
                _ComponentSpec("OTHER_REGULATED", "FNEE + retribución OS y OMIE", ComponentCategory.REGULATED),
                (OS_FEE_EUR_MWH + OMIE_FEE_EUR_MWH) * to_meter + FNEE_EUR_MWH,
            ),
            (_ComponentSpec("MARGIN", margin_label, ComponentCategory.MARGIN), float(request.margin_eur_mwh)),
        ]

        def weighted(unit: np.ndarray | float, mask: np.ndarray | None = None) -> float:
            if isinstance(unit, float):
                return unit
            if mask is None:
                return float(unit @ load / energy)
            volume = float(load[mask].sum())
            return float(unit[mask] @ load[mask] / volume) if volume > 0 else float(unit[mask].mean())

        pass_through_categories = (
            {ComponentCategory.REGULATED}
            if is_fixed
            else {ComponentCategory.REGULATED, ComponentCategory.ENERGY, ComponentCategory.BALANCING}
        )
        annual_cents = {spec.code: to_cents(weighted(unit)) for spec, unit in lines}
        build_up = [
            BuildUpComponent(
                code=spec.code,
                label=spec.label,
                category=spec.category,
                value_eur_mwh=from_cents(annual_cents[spec.code]),
                pass_through=spec.category in pass_through_categories,
            )
            for spec, _ in lines
        ]
        final_cents = sum(annual_cents.values())

        period_prices: list[PeriodPrice] = []
        tou_weighted = 0.0
        for p in range(1, table.n_periods + 1):
            mask = periods == p
            period_cents = {spec.code: to_cents(weighted(unit, mask)) for spec, unit in lines}
            volume = float(load[mask].sum())
            price_cents = sum(period_cents.values())
            tou_weighted += volume / energy * from_cents(price_cents)
            period_prices.append(
                PeriodPrice(
                    period=f"P{p}",
                    hours=int(mask.sum()),
                    consumption_mwh=round(volume, 4),
                    consumption_share_pct=round(100.0 * volume / energy, 2),
                    omie_weighted_eur_mwh=round(weighted(self.spot, mask), 2),
                    price_eur_mwh=from_cents(price_cents),
                    components={code: from_cents(c) for code, c in period_cents.items()},
                )
            )

        weighted_omie = float(self.spot @ load / energy)
        regulated_cents = sum(annual_cents[spec.code] for spec, _ in lines if spec.category is ComponentCategory.REGULATED)
        power_term = (
            round(request.contracted_power_kw * table.power_term_eur_kw_year, 2) if request.contracted_power_kw else None
        )
        kpis = PowerQuoteKpis(
            final_price_eur_mwh=from_cents(final_cents),
            baseload_omie_eur_mwh=from_cents(annual_cents["OMIE_BASELOAD"]),
            weighted_omie_eur_mwh=round(weighted_omie, 2),
            shape_cost_eur_mwh=from_cents(annual_cents["SHAPE"]),
            shape_factor=round(weighted_omie / self.baseload, 4) if self.baseload else 0.0,
            ancillary_cost_eur_mwh=from_cents(annual_cents["SSAA"]),
            imbalance_cost_eur_mwh=from_cents(annual_cents["IMBALANCE"]),
            imbalance_cost_p95_eur_mwh=None if imbalance_p95 is None else round(imbalance_p95, 2),
            risk_premium_eur_mwh=from_cents(annual_cents.get("RISK_PREMIUM", 0)),
            regulated_cost_eur_mwh=from_cents(regulated_cents),
            margin_eur_mwh=from_cents(annual_cents["MARGIN"]),
            cap_strike_eur_mwh=request.cap_strike_eur_mwh if is_cap else None,
            cap_option_premium_eur_mwh=from_cents(annual_cents["CAP_PREMIUM"]) if is_cap else None,
            cap_hours_in_the_money=int((cap_payoff > 0).sum()) if is_cap else None,
            annual_energy_bill_eur=round(from_cents(final_cents) * energy, 2),
            expected_gross_margin_eur=round(from_cents(annual_cents["MARGIN"]) * energy, 2),
            power_term_eur_year=power_term,
        )
        return PowerQuoteResponse(
            tariff=request.tariff,
            profile=request.profile,
            product=product,
            annual_consumption_mwh=round(energy, 4),
            market_year=self.market_year,
            data_source=self.data_source,
            kpis=kpis,
            build_up=build_up,
            period_prices=period_prices,
            tou_weighted_average_eur_mwh=round(tou_weighted, 4),
            tou_reconciliation_diff_eur_mwh=round(tou_weighted - from_cents(final_cents), 4),
            hourly_profile=self._hourly_profile(load),
            regulatory_reference=POWER_REGULATORY_REFERENCE,
            warnings=self._warnings(request, cap_value),
        )

    def _hourly_profile(self, load: np.ndarray) -> list[HourlyProfilePoint]:
        counts = np.bincount(self.hour, minlength=24).astype("float64")
        mean_load = np.bincount(self.hour, weights=load, minlength=24) / counts
        mean_price = np.bincount(self.hour, weights=self.spot, minlength=24) / counts
        load_ref, price_ref = float(mean_load.mean()), float(mean_price.mean())
        return [
            HourlyProfilePoint(
                hour=h,
                client_load_mw=round(float(mean_load[h]), 6),
                client_load_index=round(100.0 * float(mean_load[h]) / load_ref, 2),
                omie_price_eur_mwh=round(float(mean_price[h]), 2),
                omie_price_index=round(100.0 * float(mean_price[h]) / price_ref, 2) if price_ref else 0.0,
            )
            for h in range(24)
        ]

    def _warnings(self, request: PowerQuoteRequest, cap_value: float) -> list[str]:
        notes: list[str] = []
        if request.tariff is PowerTariff.TD_20 and request.annual_consumption_mwh > MAX_ANNUAL_MWH_20TD:
            notes.append("El consumo anual supera lo que admite una potencia de 15 kW: revisa si corresponde 3.0TD.")
        if request.product is ProductStructure.FIXED_WITH_CAP and request.cap_strike_eur_mwh < self.baseload:
            notes.append(
                f"El cap ({request.cap_strike_eur_mwh:.2f} €/MWh) está por debajo del baseload "
                f"({self.baseload:.2f} €/MWh): la opción está muy en dinero ({cap_value:.2f} €/MWh de valor histórico)."
            )
        if self.data_source == "SYNTHETIC":
            notes.append("Cotización calculada sobre mercado sintético (MOCK_DATA): no usar como precio real.")
        return notes


# --------------------------------------------------------------------------------------------------
# Motor de gas natural
# --------------------------------------------------------------------------------------------------


class GasPricingEngine:
    """Cotizador de gas RL.1-RL.4 indexado a MIBGAS PVB con estacionalidad HDD y desbalance Enagás."""

    def __init__(self, market: pd.DataFrame) -> None:
        missing = [c for c in GAS_REQUIRED_COLUMNS if c not in market.columns]
        if missing:
            raise PricingError(f"gas_market sin columnas requeridas: {missing}")
        if market.empty:
            raise PricingError("gas_market está vacío")
        self.market = market.reset_index(drop=True)
        self.pvb = self.market["pvb_da_price_eur_mwh"].to_numpy(dtype="float64")
        self.imbalance_buy = self.market["imbalance_buy_price_eur_mwh"].to_numpy(dtype="float64")
        self.imbalance_sell = self.market["imbalance_sell_price_eur_mwh"].to_numpy(dtype="float64")
        self.hdd = self.market["hdd_18c"].to_numpy(dtype="float64")
        self.month = self.market["month"].to_numpy(dtype=np.int16)
        self.winter = (self.market["gas_season"] == "WINTER").to_numpy(dtype=bool)
        self.baseload = float(self.pvb.mean())
        self.market_year = int(pd.Timestamp(self.market["gas_day"].iloc[len(self.market) // 2]).year)
        self.data_source = str(self.market["data_source"].iloc[0]) if "data_source" in self.market.columns else "UNKNOWN"

    @classmethod
    def from_warehouse(cls, warehouse_dir: Path) -> GasPricingEngine:
        return cls(load_gas_market(warehouse_dir))

    def quote(self, request: GasQuoteRequest) -> GasQuoteResponse:
        load = build_gas_profile(self.market, request.profile, request.annual_consumption_mwh)
        energy = float(load.sum())
        table = GAS_REGULATED[request.tariff]
        is_fixed = request.product is GasProduct.FIXED
        sigma = request.forecast_error_pct / 100.0
        level = request.tariff.value
        # Los peajes se facturan incrementados con la tasa de la CNMC y la cuota del GTS; los cargos, solo con la tasa.
        toll_uplift = 1.0 + GAS_CNMC_FEE_RATIO + GAS_GTS_QUOTA_RATIO

        weighted_pvb = float(self.pvb @ load / energy)
        imbalance = float((sigma * HALF_NORMAL_MEAN * (self.imbalance_buy - self.imbalance_sell)) @ load / energy)
        monthly_energy = np.bincount(self.month, weights=load, minlength=13)[1:]
        monthly_cost = np.bincount(self.month, weights=load * self.pvb, minlength=13)[1:]
        active = monthly_energy > 0
        monthly_price = monthly_cost[active] / monthly_energy[active]
        risk_premium = float(request.risk_aversion * (monthly_price.max() - np.median(monthly_price))) if is_fixed else 0.0

        lines: list[tuple[_ComponentSpec, float]] = [
            (_ComponentSpec("PVB_BASELOAD", "MIBGAS PVB baseload", ComponentCategory.ENERGY), self.baseload),
            (_ComponentSpec("SEASONAL_SHAPE", "Coste de forma estacional (HDD)", ComponentCategory.ENERGY), weighted_pvb - self.baseload),
            (_ComponentSpec("SHRINKAGE", "Mermas", ComponentCategory.ENERGY), GAS_SHRINKAGE_RATIO * weighted_pvb),
            (_ComponentSpec("IMBALANCE", "Coste de desbalance Enagás", ComponentCategory.BALANCING), imbalance),
        ]
        if is_fixed:
            lines.append((_ComponentSpec("RISK_PREMIUM", "Prima de riesgo estacional", ComponentCategory.RISK), risk_premium))
        lines += [
            (
                _ComponentSpec("TOLL_VARIABLE", f"Peaje {level} redes locales (variable)", ComponentCategory.REGULATED),
                table.local_variable_eur_mwh * toll_uplift,
            ),
            (
                _ComponentSpec("TOLL_FIXED", f"Peajes {level} término fijo (redes locales + transporte)", ComponentCategory.REGULATED),
                table.toll_fixed_eur_year * toll_uplift / energy,
            ),
            (
                _ComponentSpec("OTHER_REGULATED", "Cargos del sistema gasista + FNEE", ComponentCategory.REGULATED),
                table.charge_fixed_eur_year * (1.0 + GAS_CNMC_FEE_RATIO) / energy + GAS_FNEE_EUR_MWH,
            ),
            (_ComponentSpec("MARGIN", "Margen comercial" if is_fixed else "Fee de gestión", ComponentCategory.MARGIN), float(request.margin_eur_mwh)),
        ]
        pass_through_categories = (
            {ComponentCategory.REGULATED}
            if is_fixed
            else {ComponentCategory.REGULATED, ComponentCategory.ENERGY, ComponentCategory.BALANCING}
        )
        cents = {spec.code: to_cents(value) for spec, value in lines}
        final_cents = sum(cents.values())
        build_up = [
            BuildUpComponent(
                code=spec.code,
                label=spec.label,
                category=spec.category,
                value_eur_mwh=from_cents(cents[spec.code]),
                pass_through=spec.category in pass_through_categories,
            )
            for spec, _ in lines
        ]
        regulated_cents = sum(cents[spec.code] for spec, _ in lines if spec.category is ComponentCategory.REGULATED)
        month_days = np.bincount(self.month, minlength=13)[1:]
        monthly = [
            GasMonthlyPoint(
                month=m + 1,
                consumption_mwh=round(float(monthly_energy[m]), 4),
                consumption_share_pct=round(100.0 * float(monthly_energy[m]) / energy, 2),
                pvb_avg_eur_mwh=round(float(np.bincount(self.month, weights=self.pvb, minlength=13)[m + 1] / month_days[m]), 2),
                hdd=round(float(np.bincount(self.month, weights=self.hdd, minlength=13)[m + 1]), 1),
            )
            for m in range(12)
            if month_days[m] > 0
        ]
        kpis = GasQuoteKpis(
            final_price_eur_mwh=from_cents(final_cents),
            baseload_pvb_eur_mwh=from_cents(cents["PVB_BASELOAD"]),
            weighted_pvb_eur_mwh=round(weighted_pvb, 2),
            seasonal_shape_cost_eur_mwh=from_cents(cents["SEASONAL_SHAPE"]),
            shape_factor=round(weighted_pvb / self.baseload, 4) if self.baseload else 0.0,
            imbalance_cost_eur_mwh=from_cents(cents["IMBALANCE"]),
            risk_premium_eur_mwh=from_cents(cents.get("RISK_PREMIUM", 0)),
            regulated_cost_eur_mwh=from_cents(regulated_cents),
            margin_eur_mwh=from_cents(cents["MARGIN"]),
            winter_consumption_share_pct=round(100.0 * float(load[self.winter].sum()) / energy, 2),
            annual_bill_eur=round(from_cents(final_cents) * energy, 2),
            expected_gross_margin_eur=round(from_cents(cents["MARGIN"]) * energy, 2),
        )
        warnings: list[str] = []
        if not table.min_mwh < request.annual_consumption_mwh <= table.max_mwh:
            warnings.append(
                f"El consumo anual ({request.annual_consumption_mwh:g} MWh) queda fuera de la banda de {request.tariff.value} "
                f"({table.min_mwh:g}-{table.max_mwh:g} MWh/año)."
            )
        if self.data_source == "SYNTHETIC":
            warnings.append("Cotización calculada sobre mercado sintético (MOCK_DATA): no usar como precio real.")
        return GasQuoteResponse(
            tariff=request.tariff,
            profile=request.profile,
            product=request.product,
            annual_consumption_mwh=round(energy, 4),
            market_year=self.market_year,
            data_source=self.data_source,
            kpis=kpis,
            build_up=build_up,
            monthly=monthly,
            regulatory_reference=GAS_REGULATORY_REFERENCE,
            warnings=warnings,
        )
