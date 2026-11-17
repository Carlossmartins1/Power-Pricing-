"""Casos límite: toda combinación de tarifa, producto y perfil cuadra al céntimo, también con precios OMIE
negativos, nulos o extremos, y las entradas inválidas se rechazan con un error explícito."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.core.pricing_engine import GasPricingEngine, PowerPricingEngine, PricingError, to_cents
from app.models.tariffs import (
    GasProduct,
    GasProfileType,
    GasQuoteRequest,
    GasTariff,
    ImbalanceMethod,
    LoadProfileType,
    PowerQuoteRequest,
    PowerTariff,
    ProductStructure,
)
from app.pipelines.market_ingestion import SyntheticMarket, SyntheticMarketGenerator

POWER_PRICE_COLUMNS = ("spot_price_eur_mwh", "imbalance_price_up_eur_mwh", "imbalance_price_down_eur_mwh")
# Escenarios de mercado: transformación de los precios horarios (spot y desvíos) del año sintético.
MARKETS: dict[str, Callable[[pd.Series], pd.Series]] = {
    "base": lambda prices: prices,
    "omie_negativo": lambda prices: prices - 250.0,  # baseload por debajo de cero todo el año
    "omie_extremo": lambda prices: prices * 25.0,  # crisis de precios: baseload por encima de 1.700 €/MWh
    "omie_cero": lambda prices: prices * 0.0,  # baseload exactamente cero
}


@pytest.fixture(scope="module")
def market() -> SyntheticMarket:
    return SyntheticMarketGenerator(
        year=2025, seed=42, tz="Europe/Madrid", reinforced_operation_start=date(2025, 4, 28), small_adjustment=0.03
    ).generate()


@pytest.fixture(scope="module")
def custom_curve(market: SyntheticMarket) -> list[float]:
    return np.random.default_rng(7).uniform(0.0, 5.0, len(market.power)).tolist()


def _stressed_engine(market: SyntheticMarket, scenario: str) -> PowerPricingEngine:
    frame = market.power.copy()
    for column in POWER_PRICE_COLUMNS:
        frame[column] = MARKETS[scenario](frame[column])
    return PowerPricingEngine(frame)


def _assert_reconciles(build_up_values: list[float], final_price: float) -> None:
    assert all(np.isfinite(value) for value in build_up_values)
    assert sum(to_cents(value) for value in build_up_values) == to_cents(final_price)


@pytest.mark.parametrize("scenario", list(MARKETS))
def test_every_power_combination_reconciles_to_the_cent(market: SyntheticMarket, custom_curve: list[float], scenario: str) -> None:
    engine = _stressed_engine(market, scenario)
    for tariff, product, profile in itertools.product(PowerTariff, ProductStructure, LoadProfileType):
        request = PowerQuoteRequest(
            tariff=tariff,
            product=product,
            profile=profile,
            custom_profile=custom_curve if profile is LoadProfileType.CUSTOM else None,
            contracted_power_kw=50.0,
        )
        quote = engine.quote(request)
        _assert_reconciles([line.value_eur_mwh for line in quote.build_up], quote.kpis.final_price_eur_mwh)
        for period in quote.period_prices:
            _assert_reconciles(list(period.components.values()), period.price_eur_mwh)
        # La media ponderada de los precios por periodo solo se separa del precio anual por el redondeo de cada periodo.
        assert abs(quote.tou_reconciliation_diff_eur_mwh) < 0.05
        assert sum(p.hours for p in quote.period_prices) == len(market.power)
        assert np.isfinite(quote.kpis.shape_factor)


@pytest.mark.parametrize("scenario", ["base", "omie_negativo"])
@pytest.mark.parametrize("tariff", list(PowerTariff))
def test_monte_carlo_imbalance_reconciles_with_negative_prices(market: SyntheticMarket, scenario: str, tariff: PowerTariff) -> None:
    engine = _stressed_engine(market, scenario)
    request = PowerQuoteRequest(tariff=tariff, imbalance_method=ImbalanceMethod.MONTE_CARLO, monte_carlo_paths=50)
    quote = engine.quote(request)
    _assert_reconciles([line.value_eur_mwh for line in quote.build_up], quote.kpis.final_price_eur_mwh)
    # El coste de desvíos depende de los diferenciales (bajar − spot, spot − subir), no del nivel de precios.
    assert quote.kpis.imbalance_cost_eur_mwh >= 0.0
    assert quote.kpis.imbalance_cost_p95_eur_mwh is not None and quote.kpis.imbalance_cost_p95_eur_mwh >= quote.kpis.imbalance_cost_eur_mwh


def test_cap_has_no_value_when_prices_never_reach_the_strike(market: SyntheticMarket) -> None:
    quote = _stressed_engine(market, "omie_negativo").quote(PowerQuoteRequest(product=ProductStructure.FIXED_WITH_CAP))
    lines = {line.code: line.value_eur_mwh for line in quote.build_up}
    assert lines["CAP_PAYOFF"] == 0.0 and lines["CAP_PREMIUM"] == 0.0
    assert quote.kpis.cap_hours_in_the_money == 0


@pytest.mark.parametrize(
    ("tariff", "profile", "product", "volume"),
    list(itertools.product(GasTariff, GasProfileType, GasProduct, (0.01, 3.0, 50.0, 300.0, 100_000.0))),
)
def test_every_gas_combination_reconciles_to_the_cent(
    market: SyntheticMarket, tariff: GasTariff, profile: GasProfileType, product: GasProduct, volume: float
) -> None:
    quote = GasPricingEngine(market.gas).quote(GasQuoteRequest(tariff=tariff, profile=profile, product=product, annual_consumption_mwh=volume))
    _assert_reconciles([line.value_eur_mwh for line in quote.build_up], quote.kpis.final_price_eur_mwh)


def test_gas_engine_survives_a_zero_price_year(market: SyntheticMarket) -> None:
    frame = market.gas.copy()
    for column in ("pvb_da_price_eur_mwh", "imbalance_buy_price_eur_mwh", "imbalance_sell_price_eur_mwh"):
        frame[column] = 0.0
    quote = GasPricingEngine(frame).quote(GasQuoteRequest())
    assert quote.kpis.shape_factor == 0.0
    _assert_reconciles([line.value_eur_mwh for line in quote.build_up], quote.kpis.final_price_eur_mwh)


def test_single_hour_custom_curve_is_priced_at_that_hour(market: SyntheticMarket) -> None:
    engine = PowerPricingEngine(market.power)
    curve = [0.0] * len(market.power)
    curve[4000] = 1.0
    quote = engine.quote(PowerQuoteRequest(profile=LoadProfileType.CUSTOM, custom_profile=curve, annual_consumption_mwh=1.0))
    assert quote.kpis.weighted_omie_eur_mwh == pytest.approx(float(engine.spot[4000]), abs=0.01)
    assert sum(1 for period in quote.period_prices if period.consumption_mwh > 0) == 1


@pytest.mark.parametrize(
    "curve_factory",
    [
        lambda n: [0.0] * n,  # sin consumo
        lambda n: [1.0] * (n + 24),  # un día de más (año bisiesto)
        lambda n: [1.0] * (n - 1),  # una hora de menos
        lambda n: [-1.0] + [1.0] * (n - 1),  # valor negativo (excedente vertido)
        lambda n: [float("inf")] + [1.0] * (n - 1),
        lambda n: [float("nan")] + [1.0] * (n - 1),
    ],
    ids=["ceros", "8784_valores", "8759_valores", "negativo", "infinito", "nan"],
)
def test_invalid_custom_curves_are_rejected(market: SyntheticMarket, curve_factory: Callable[[int], list[float]]) -> None:
    engine = PowerPricingEngine(market.power)
    with pytest.raises(PricingError):
        engine.quote(PowerQuoteRequest(profile=LoadProfileType.CUSTOM, custom_profile=curve_factory(len(market.power))))
