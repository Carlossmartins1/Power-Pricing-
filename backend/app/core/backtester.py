"""Back-testing de cartera minorista, escenarios de estrés y atribución de P&L (Fase 3).

Compara el margen bruto esperado al firmar (ex-ante) con el realizado sobre el periodo ya entregado (ex-post)
para una cartera sintética de contratos de electricidad (2.0TD, 3.0TD, 6.1TD) y gas (RL.1-RL.3), y reconcilia
la diferencia en cuatro efectos que suman exactamente:

    M_ex-post - M_ex-ante = Volumen + Perfil (shape) + Mayorista/cobertura + Desvíos

Con V0/V1 el volumen contratado/real, p0/p1 la forma horaria normalizada contratada/real, F la curva forward al
firmar, S el spot realizado, A los servicios de ajuste, pi la prima de desvíos cobrada, u la fracción no
cubierta e I1 el coste real de desvíos, para un contrato a precio fijo:

    Volumen    = M0 * (V1/V0 - 1)                      más o menos energía vendida al margen unitario pactado
    Perfil     = -V1 * sum((p1 - p0) * (F + A))        la curva real pesa más o menos en las horas caras
    Mayorista  = -sum((V1*p1 - (1-u)*V0*p0) * (S - F)) posición abierta liquidada a spot en vez de a forward
    Desvíos    = pi*V1 - I1                            prima cobrada frente a coste real REE / Enagás

En contratos indexados la energía se repercute a spot, así que Perfil y Mayorista son cero por construcción.

Supuestos de modelo (el warehouse contiene un único año de mercado):
* La curva forward ex-ante se reconstruye a partir del realizado: forma media por (mes, tipo de día, hora) y
  un sesgo mensual determinista por cohorte de firma (12 meses: sigma 7 %, 6 meses: sigma 4 %).
* Los contratos firmados hace 12 meses entregan el año completo; los de 6 meses, de julio a diciembre.
* Peajes, cargos y pérdidas se repercuten a coste y no generan margen: quedan fuera del P&L.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from app.core.pricing_engine import (
    GAS_PROFILE_PARAMETERS,
    GAS_REQUIRED_COLUMNS,
    HALF_NORMAL_MEAN,
    POWER_REQUIRED_COLUMNS,
    PricingError,
    build_gas_profile,
    build_load_profile,
)
from app.models.portfolio import (
    AlertLevel,
    BacktestKpis,
    BacktestRequest,
    BacktestResponse,
    Commodity,
    ContractProduct,
    ContractResult,
    PnlAttribution,
    SegmentSummary,
    StressScenario,
    WaterfallStep,
)
from app.models.tariffs import GasProfileType, LoadProfileType
from app.pipelines.market_ingestion import load_gas_market, load_power_market

FORWARD_BIAS_STD: Final[dict[int, float]] = {12: 0.07, 6: 0.04}
FORWARD_BIAS_AUTOCORRELATION: Final[float] = 0.5
RISK_PREMIUM_RATIO: Final[float] = 0.015  # prima de riesgo ex-ante: 1,5 % del coste forward de energía
UNFORECAST_VOLUME_SHARE: Final[float] = 0.25  # parte de la desviación de volumen que acaba liquidada como desvío
GAS_WEATHER_SENSITIVITY: Final[float] = 1.5  # la demanda térmica reacciona 1,5x al shock climático
SIX_MONTH_FIRST_DELIVERY_MONTH: Final[int] = 7
MIN_VOLUME_RATIO: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class PortfolioSegment:
    tariff: str
    commodity: Commodity
    share: float
    median_mwh: float
    log_sigma: float
    min_mwh: float
    max_mwh: float
    profiles: tuple[tuple[str, float], ...]
    margin: tuple[float, float, float, float]  # media, desviación, mínimo, máximo (€/MWh)
    forecast_error_pct: tuple[float, float]
    weather_sensitivity: float
    indexed_share: float


PORTFOLIO_SEGMENTS: Final[tuple[PortfolioSegment, ...]] = (
    PortfolioSegment(
        "2.0TD", Commodity.POWER, 0.52, 3.2, 0.45, 0.8, 15.0,
        ((LoadProfileType.RESIDENTIAL_EVENING.value, 0.75), (LoadProfileType.SOLAR_PROSUMER.value, 0.25)),
        (6.0, 2.0, 1.5, 12.0), (8.0, 14.0), 1.2, 0.10,
    ),
    PortfolioSegment(
        "3.0TD", Commodity.POWER, 0.20, 120.0, 0.80, 15.0, 1500.0,
        (
            (LoadProfileType.COMMERCIAL_DAY.value, 0.70),
            (LoadProfileType.SOLAR_PROSUMER.value, 0.15),
            (LoadProfileType.INDUSTRIAL_FLAT.value, 0.15),
        ),
        (4.0, 1.5, 1.0, 9.0), (5.0, 9.0), 1.0, 0.30,
    ),
    PortfolioSegment(
        "6.1TD", Commodity.POWER, 0.06, 3500.0, 0.70, 400.0, 40000.0,
        ((LoadProfileType.INDUSTRIAL_FLAT.value, 0.70), (LoadProfileType.COMMERCIAL_DAY.value, 0.30)),
        (2.2, 0.8, 0.6, 5.0), (2.0, 5.0), 0.3, 0.50,
    ),
    PortfolioSegment(
        "RL.1", Commodity.GAS, 0.10, 3.0, 0.30, 0.8, 5.0,
        ((GasProfileType.RESIDENTIAL_HEATING.value, 1.0),),
        (5.0, 1.5, 1.5, 10.0), (8.0, 14.0), GAS_WEATHER_SENSITIVITY, 0.05,
    ),
    PortfolioSegment(
        "RL.2", Commodity.GAS, 0.08, 9.0, 0.25, 5.1, 15.0,
        ((GasProfileType.RESIDENTIAL_HEATING.value, 0.80), (GasProfileType.COMMERCIAL.value, 0.20)),
        (4.0, 1.2, 1.0, 8.0), (7.0, 12.0), GAS_WEATHER_SENSITIVITY, 0.10,
    ),
    PortfolioSegment(
        "RL.3", Commodity.GAS, 0.04, 28.0, 0.30, 15.1, 50.0,
        ((GasProfileType.COMMERCIAL.value, 0.70), (GasProfileType.INDUSTRIAL_PROCESS.value, 0.30)),
        (3.0, 1.0, 0.8, 6.0), (5.0, 10.0), GAS_WEATHER_SENSITIVITY, 0.20,
    ),
)


@dataclass(frozen=True, slots=True)
class ContractCurves:
    """Curvas de un tipo de contrato (commodity, perfil, cohorte) sobre su ventana de entrega."""

    contracted_shape: np.ndarray  # p0: suma 1 en la ventana
    realized_shape: np.ndarray  # p1: suma 1 en la ventana
    window_share: float  # fracción del consumo anual que cae en la ventana
    weather_volume_ratio: float  # V1/V0 inducido por el clima vía la forma (gas); 1,0 en electricidad
    forward: np.ndarray  # F: curva forward al firmar
    realized: np.ndarray  # S: spot realizado (con shock mayorista)
    ancillary: np.ndarray  # A: servicios de ajuste (0 en gas)
    imbalance_spread: np.ndarray  # precio corto - precio largo (ya multiplicado por el estrés)


@dataclass(frozen=True, slots=True)
class _CurveStats:
    window_share: float
    weather_volume_ratio: float
    forward_cost_contracted: float  # sum(p0 * (F + A))
    forward_cost_realized: float  # sum(p1 * (F + A))
    forward_energy: float  # sum(p0 * F)
    basis_contracted: float  # sum(p0 * (S - F))
    basis_realized: float  # sum(p1 * (S - F))
    spread_priced: float  # sum(p0 * D) con spreads sin estresar
    spread_realized: float  # sum(p1 * D) con el multiplicador de estrés


def _cents(value: float) -> int:
    return int(round(value * 100.0))


class PortfolioBacktester:
    """Genera la cartera, aplica el escenario de estrés y atribuye el P&L contrato a contrato."""

    def __init__(self, power_market: pd.DataFrame, gas_market: pd.DataFrame) -> None:
        for name, frame, required in (
            ("power_market", power_market, POWER_REQUIRED_COLUMNS),
            ("gas_market", gas_market, GAS_REQUIRED_COLUMNS),
        ):
            missing = [c for c in required if c not in frame.columns]
            if missing:
                raise PricingError(f"{name} sin columnas requeridas: {missing}")
        self.power = power_market.reset_index(drop=True)
        self.gas = gas_market.reset_index(drop=True)

        self._spot = self.power["spot_price_eur_mwh"].to_numpy(dtype="float64")
        self._ancillary = self.power["ancillary_services_cost_eur_mwh"].to_numpy(dtype="float64")
        self._power_spread = (
            self.power["imbalance_price_down_eur_mwh"] - self.power["imbalance_price_up_eur_mwh"]
        ).to_numpy(dtype="float64")
        self._power_month = self.power["month"].to_numpy(dtype=np.int16)
        off_day = self.power["is_weekend"] | self.power["is_tariff_holiday"]
        self._hpfc_shape = (
            self.power.assign(_off=off_day)
            .groupby(["month", "_off", "hour_local"])["spot_price_eur_mwh"]
            .transform("mean")
            .to_numpy(dtype="float64")
        )
        hour = self.power["hour_local"].to_numpy(dtype="float64") + 0.5
        self._solar_weight = np.exp(-0.5 * ((hour - 13.5) / 2.5) ** 2)

        self._pvb = self.gas["pvb_da_price_eur_mwh"].to_numpy(dtype="float64")
        self._gas_spread = (
            self.gas["imbalance_buy_price_eur_mwh"] - self.gas["imbalance_sell_price_eur_mwh"]
        ).to_numpy(dtype="float64")
        self._gas_month = self.gas["month"].to_numpy(dtype=np.int16)
        self._gas_monthly_mean = self.gas.groupby("month")["pvb_da_price_eur_mwh"].transform("mean").to_numpy(dtype="float64")
        self._gas_weekend = self.gas["is_weekend"].to_numpy(dtype=bool)
        self._hdd = self.gas["hdd_18c"].to_numpy(dtype="float64")

        self._power_profiles = {
            p.value: build_load_profile(self.power, p, 1.0) for p in LoadProfileType if p is not LoadProfileType.CUSTOM
        }
        self._gas_profiles = {p.value: build_gas_profile(self.gas, p, 1.0) for p in GasProfileType}
        self.market_year = int(pd.Timestamp(self.power["datetime_local"].iloc[len(self.power) // 2]).year)
        self.data_source = str(self.power["data_source"].iloc[0]) if "data_source" in self.power.columns else "UNKNOWN"

    @classmethod
    def from_warehouse(cls, warehouse_dir: Path) -> PortfolioBacktester:
        return cls(load_power_market(warehouse_dir), load_gas_market(warehouse_dir))

    # ---------------- cartera ----------------

    @staticmethod
    def generate_portfolio(n_contracts: int, seed: int) -> pd.DataFrame:
        """Cartera sintética reproducible: mezcla de segmentos, volúmenes log-normales y precios cerrados ex-ante."""
        rng = np.random.default_rng(seed)
        shares = np.array([s.share for s in PORTFOLIO_SEGMENTS])
        counts = np.floor(shares * n_contracts).astype(int)
        for i in range(n_contracts - int(counts.sum())):
            counts[np.argsort(-shares)[i % len(shares)]] += 1
        frames: list[pd.DataFrame] = []
        for segment, count in zip(PORTFOLIO_SEGMENTS, counts):
            if count == 0:
                continue
            names = [name for name, _ in segment.profiles]
            weights = np.array([w for _, w in segment.profiles])
            mean, std, low, high = segment.margin
            frames.append(
                pd.DataFrame(
                    {
                        "commodity": segment.commodity.value,
                        "tariff": segment.tariff,
                        "profile": rng.choice(names, size=count, p=weights / weights.sum()),
                        "product": np.where(
                            rng.random(count) < segment.indexed_share, ContractProduct.INDEXED.value, ContractProduct.FIXED.value
                        ),
                        "signed_months_ago": np.where(rng.random(count) < 0.55, 12, 6),
                        "annual_mwh": np.clip(
                            segment.median_mwh * np.exp(segment.log_sigma * rng.standard_normal(count)),
                            segment.min_mwh,
                            segment.max_mwh,
                        ),
                        "margin_eur_mwh": np.round(np.clip(rng.normal(mean, std, count), low, high), 2),
                        "forecast_error_pct": np.round(rng.uniform(*segment.forecast_error_pct, count), 1),
                        "weather_sensitivity": segment.weather_sensitivity,
                        "volume_noise": np.clip(rng.normal(0.0, 0.06, count), -0.20, 0.20),
                        "error_factor": rng.lognormal(0.0, 0.25, count),
                    }
                )
            )
        portfolio = pd.concat(frames, ignore_index=True)
        prefix = np.where(portfolio["commodity"] == Commodity.POWER.value, "PW", "GS")
        portfolio.insert(0, "contract_id", [f"{p}-{i:04d}" for i, p in enumerate(prefix, start=1)])
        return portfolio

    # ---------------- curvas de mercado ----------------

    @staticmethod
    def _monthly_bias(commodity: Commodity, cohort: int, seed: int) -> np.ndarray:
        """Sesgo forward-vs-realizado por mes de entrega (AR(1), determinista por semilla, commodity y cohorte)."""
        rng = np.random.default_rng([seed, 0 if commodity is Commodity.POWER else 1, cohort])
        shocks = rng.normal(0.0, FORWARD_BIAS_STD[cohort], 12)
        bias = np.empty(12)
        bias[0] = shocks[0]
        phi = FORWARD_BIAS_AUTOCORRELATION
        for m in range(1, 12):
            bias[m] = phi * bias[m - 1] + math.sqrt(1.0 - phi**2) * shocks[m]
        return bias

    def forward_curve(self, commodity: Commodity, cohort: int, seed: int) -> np.ndarray:
        bias = self._monthly_bias(commodity, cohort, seed)
        if commodity is Commodity.POWER:
            return self._hpfc_shape * (1.0 + bias[self._power_month - 1])
        return self._gas_monthly_mean * (1.0 + bias[self._gas_month - 1])

    def realized_curve(self, commodity: Commodity, stress: StressScenario) -> np.ndarray:
        base = self._spot if commodity is Commodity.POWER else self._pvb
        return base * (1.0 + stress.wholesale_price_shock_pct / 100.0)

    def contract_curves(self, commodity: Commodity, profile: str, cohort: int, stress: StressScenario, seed: int) -> ContractCurves:
        if cohort not in FORWARD_BIAS_STD:
            raise PricingError(f"Cohorte de firma no soportada: {cohort} meses")
        if commodity is Commodity.POWER:
            window = self._power_month >= (SIX_MONTH_FIRST_DELIVERY_MONTH if cohort == 6 else 1)
            contracted = self._power_profiles[profile] * window
            realized = contracted * (1.0 - stress.shape_shift_pct / 100.0 * self._solar_weight)
            ratio = 1.0
            ancillary, spread = self._ancillary, self._power_spread
        else:
            window = self._gas_month >= (SIX_MONTH_FIRST_DELIVERY_MONTH if cohort == 6 else 1)
            base_share, weekend_factor = GAS_PROFILE_PARAMETERS[GasProfileType(profile)]
            flat = np.where(self._gas_weekend, weekend_factor, 1.0)
            flat = flat / flat.sum()
            heating = self._hdd / self._hdd.sum()
            weather = max(1.0 + stress.volume_shock_pct / 100.0 * GAS_WEATHER_SENSITIVITY, MIN_VOLUME_RATIO)
            contracted = self._gas_profiles[profile] * window
            realized = (base_share * flat + (1.0 - base_share) * heating * weather) * window
            ratio = float(realized.sum() / contracted.sum())
            ancillary, spread = np.zeros(len(self.gas)), self._gas_spread
        window_share = float(contracted.sum())
        return ContractCurves(
            contracted_shape=contracted / window_share,
            realized_shape=realized / realized.sum(),
            window_share=window_share,
            weather_volume_ratio=ratio,
            forward=self.forward_curve(commodity, cohort, seed),
            realized=self.realized_curve(commodity, stress),
            ancillary=ancillary,
            imbalance_spread=spread * stress.imbalance_multiplier,
        )

    def _curve_stats(self, commodity: Commodity, profile: str, cohort: int, stress: StressScenario, seed: int) -> _CurveStats:
        c = self.contract_curves(commodity, profile, cohort, stress, seed)
        p0, p1 = c.contracted_shape, c.realized_shape
        unstressed_spread = self._power_spread if commodity is Commodity.POWER else self._gas_spread
        return _CurveStats(
            window_share=c.window_share,
            weather_volume_ratio=c.weather_volume_ratio,
            forward_cost_contracted=float(p0 @ (c.forward + c.ancillary)),
            forward_cost_realized=float(p1 @ (c.forward + c.ancillary)),
            forward_energy=float(p0 @ c.forward),
            basis_contracted=float(p0 @ (c.realized - c.forward)),
            basis_realized=float(p1 @ (c.realized - c.forward)),
            spread_priced=float(p0 @ unstressed_spread),
            spread_realized=float(p1 @ c.imbalance_spread),
        )

    # ---------------- back-test ----------------

    def evaluate(self, portfolio: pd.DataFrame, stress: StressScenario, seed: int) -> pd.DataFrame:
        """P&L ex-ante, ex-post y sus cuatro efectos para cada contrato (vectorizado por tipo de curva)."""
        keys = list(zip(portfolio["commodity"], portfolio["profile"], portfolio["signed_months_ago"]))
        cache = {key: self._curve_stats(Commodity(key[0]), key[1], int(key[2]), stress, seed) for key in set(keys)}

        def column(name: str) -> np.ndarray:
            return np.array([getattr(cache[key], name) for key in keys], dtype="float64")

        fixed = (portfolio["product"] == ContractProduct.FIXED.value).to_numpy()
        is_power = (portfolio["commodity"] == Commodity.POWER.value).to_numpy()
        margin = portfolio["margin_eur_mwh"].to_numpy(dtype="float64")
        sigma = portfolio["forecast_error_pct"].to_numpy(dtype="float64") / 100.0
        unhedged = stress.unhedged_ratio

        contracted = portfolio["annual_mwh"].to_numpy(dtype="float64") * column("window_share")
        weather = np.where(
            is_power,
            1.0 + stress.volume_shock_pct / 100.0 * portfolio["weather_sensitivity"].to_numpy(dtype="float64"),
            column("weather_volume_ratio"),
        )
        volume_ratio = np.maximum(weather + portfolio["volume_noise"].to_numpy(dtype="float64"), MIN_VOLUME_RATIO)
        realized = contracted * volume_ratio

        imbalance_premium = sigma * HALF_NORMAL_MEAN * column("spread_priced")
        risk_premium = np.where(fixed, RISK_PREMIUM_RATIO * column("forward_energy"), 0.0)
        price = column("forward_cost_contracted") + imbalance_premium + risk_premium + margin
        realized_sigma = sigma * portfolio["error_factor"].to_numpy(dtype="float64") + UNFORECAST_VOLUME_SHARE * np.abs(volume_ratio - 1.0)
        imbalance_cost = realized * realized_sigma * HALF_NORMAL_MEAN * column("spread_realized")

        ex_ante = contracted * (margin + risk_premium)
        volume_effect = ex_ante * (volume_ratio - 1.0)
        shape_effect = np.where(fixed, -realized * (column("forward_cost_realized") - column("forward_cost_contracted")), 0.0)
        wholesale_effect = np.where(
            fixed, -(realized * column("basis_realized") - (1.0 - unhedged) * contracted * column("basis_contracted")), 0.0
        )
        imbalance_effect = imbalance_premium * realized - imbalance_cost
        ex_post = ex_ante + volume_effect + shape_effect + wholesale_effect + imbalance_effect

        result = portfolio[["contract_id", "commodity", "tariff", "profile", "product", "signed_months_ago"]].copy()
        result["contracted_mwh"] = contracted
        result["realized_mwh"] = realized
        result["contract_price_eur_mwh"] = price
        result["ex_ante_margin_eur"] = ex_ante
        result["ex_post_margin_eur"] = ex_post
        result["volume_effect_eur"] = volume_effect
        result["shape_effect_eur"] = shape_effect
        result["wholesale_hedging_effect_eur"] = wholesale_effect
        result["imbalance_effect_eur"] = imbalance_effect
        result["ex_ante_unit_margin_eur_mwh"] = ex_ante / contracted
        result["realized_unit_margin_eur_mwh"] = ex_post / realized
        result["open_position_mwh"] = np.where(fixed, unhedged * contracted, 0.0)
        return result

    def run(self, request: BacktestRequest) -> BacktestResponse:
        portfolio = self.generate_portfolio(request.n_contracts, request.seed)
        result = self.evaluate(portfolio, request.stress, request.seed)
        at_risk = result["realized_unit_margin_eur_mwh"] < request.margin_threshold_eur_mwh
        result["alert"] = np.where(at_risk, AlertLevel.REPRICE_REQUIRED.value, AlertLevel.OK.value)

        ex_ante_c = _cents(float(result["ex_ante_margin_eur"].sum()))
        effects = [
            ("volume_effect_eur", "Efecto volumen"),
            ("shape_effect_eur", "Efecto perfil (shape)"),
            ("wholesale_hedging_effect_eur", "Mayorista y cobertura"),
            ("imbalance_effect_eur", "Desvíos REE / Enagás"),
        ]
        effect_c = {key: _cents(float(result[key].sum())) for key, _ in effects}
        ex_post_c = ex_ante_c + sum(effect_c.values())
        waterfall = [WaterfallStep(key="ex_ante", label="Margen ex-ante", kind="total", value_eur=ex_ante_c / 100, start_eur=0.0, end_eur=ex_ante_c / 100)]
        running = ex_ante_c
        for key, label in effects:
            waterfall.append(
                WaterfallStep(key=key, label=label, kind="effect", value_eur=effect_c[key] / 100, start_eur=running / 100, end_eur=(running + effect_c[key]) / 100)
            )
            running += effect_c[key]
        waterfall.append(WaterfallStep(key="ex_post", label="Margen ex-post", kind="total", value_eur=ex_post_c / 100, start_eur=0.0, end_eur=ex_post_c / 100))

        contracted, realized = float(result["contracted_mwh"].sum()), float(result["realized_mwh"].sum())
        kpis = BacktestKpis(
            contracts=len(result),
            contracted_mwh=round(contracted, 2),
            realized_mwh=round(realized, 2),
            ex_ante_margin_eur=ex_ante_c / 100,
            ex_post_margin_eur=ex_post_c / 100,
            margin_delta_eur=(ex_post_c - ex_ante_c) / 100,
            margin_delta_pct=round(100.0 * (ex_post_c - ex_ante_c) / ex_ante_c, 2) if ex_ante_c else 0.0,
            ex_ante_unit_margin_eur_mwh=round(ex_ante_c / 100 / contracted, 2),
            ex_post_unit_margin_eur_mwh=round(ex_post_c / 100 / realized, 2),
            contracts_at_risk=int(at_risk.sum()),
            at_risk_volume_share_pct=round(100.0 * float(result.loc[at_risk, "realized_mwh"].sum()) / realized, 2),
            open_position_mwh=round(float(result["open_position_mwh"].sum()), 2),
        )
        segments = [
            SegmentSummary(
                tariff=str(tariff),
                commodity=Commodity(group["commodity"].iloc[0]),
                contracts=len(group),
                contracted_mwh=round(float(group["contracted_mwh"].sum()), 2),
                realized_mwh=round(float(group["realized_mwh"].sum()), 2),
                ex_ante_margin_eur=round(float(group["ex_ante_margin_eur"].sum()), 2),
                ex_post_margin_eur=round(float(group["ex_post_margin_eur"].sum()), 2),
                realized_unit_margin_eur_mwh=round(float(group["ex_post_margin_eur"].sum() / group["realized_mwh"].sum()), 2),
                contracts_at_risk=int((group["alert"] == AlertLevel.REPRICE_REQUIRED.value).sum()),
            )
            for tariff, group in result.groupby("tariff", sort=False)
        ]
        money_columns = [c for c in result.columns if c.endswith(("_eur", "_eur_mwh"))]
        worst = result[at_risk].sort_values("realized_unit_margin_eur_mwh").head(request.max_alerts)
        alerts = [
            ContractResult(
                **{
                    **row,
                    **{c: round(float(row[c]), 2) for c in money_columns},
                    "contracted_mwh": round(float(row["contracted_mwh"]), 3),
                    "realized_mwh": round(float(row["realized_mwh"]), 3),
                }
            )
            for row in worst.drop(columns=["open_position_mwh"]).to_dict(orient="records")
        ]
        return BacktestResponse(
            market_year=self.market_year,
            data_source=self.data_source,
            stress=request.stress,
            margin_threshold_eur_mwh=request.margin_threshold_eur_mwh,
            kpis=kpis,
            attribution=PnlAttribution(**{key: effect_c[key] / 100 for key, _ in effects}),
            waterfall=waterfall,
            reconciliation_error_eur=round(float(result["ex_post_margin_eur"].sum()) - ex_post_c / 100, 2) + 0.0,  # evita -0.0
            segments=segments,
            at_risk_contracts=alerts,
            assumptions=[
                "Cartera sintética reproducible por semilla; volúmenes y márgenes por segmento son supuestos del modelo.",
                "Curva forward ex-ante reconstruida desde el realizado con sesgo mensual por cohorte (12 m: 7 %, 6 m: 4 %).",
                "Contratos de 12 meses entregan el año completo; los de 6 meses, de julio a diciembre.",
                "Peajes, cargos y pérdidas se repercuten a coste y quedan fuera del margen.",
                f"El {UNFORECAST_VOLUME_SHARE:.0%} de la desviación de volumen no se anticipa y se liquida como desvío.",
            ],
        )
