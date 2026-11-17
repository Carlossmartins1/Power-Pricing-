"""Tests de los parámetros regulados: valores oficiales fijados, unidades y registro de procedencia."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.core import regulated
from app.core.config import MibgasSettings
from app.core.pricing_engine import PowerPricingEngine
from app.core.regulated import (
    FNEE_EUR_MWH,
    GAS_CNMC_FEE_RATIO,
    GAS_IMBALANCE_SMALL_ADJUSTMENT,
    GAS_REGULATED,
    OMIE_FEE_EUR_MWH,
    OS_FEE_EUR_MWH,
    POWER_REGULATED,
    REGULATORY_PARAMETERS,
    ParameterStatus,
    regulatory_register,
)
from app.models.tariffs import GasTariff, LoadProfileType, PowerQuoteRequest, PowerTariff, ProductStructure
from app.pipelines.market_ingestion import SyntheticMarketGenerator


@pytest.fixture(scope="module")
def power() -> PowerPricingEngine:
    market = SyntheticMarketGenerator(
        year=2025, seed=42, tz="Europe/Madrid", reinforced_operation_start=date(2025, 4, 28), small_adjustment=0.03
    ).generate()
    return PowerPricingEngine(market.power)


# --------------------------------------------------------------------------------------------------
# Valores oficiales (si cambia una cifra, tiene que cambiar también la norma citada en regulated.py)
# --------------------------------------------------------------------------------------------------


def test_loss_coefficients_match_circular_3_2020() -> None:
    assert POWER_REGULATED[PowerTariff.TD_20].losses == (0.167, 0.163, 0.180)
    assert POWER_REGULATED[PowerTariff.TD_30].losses == (0.166, 0.175, 0.165, 0.165, 0.138, 0.180)
    assert POWER_REGULATED[PowerTariff.TD_61].losses == (0.067, 0.068, 0.065, 0.065, 0.043, 0.077)


def test_capacity_payments_match_orden_ted_1524_2025() -> None:
    assert POWER_REGULATED[PowerTariff.TD_20].capacity_payment == (0.800, 0.133, 0.000)
    assert POWER_REGULATED[PowerTariff.TD_30].capacity_payment == (1.082, 0.500, 0.333, 0.250, 0.250, 0.000)
    assert POWER_REGULATED[PowerTariff.TD_61].capacity_payment == (0.465, 0.213, 0.142, 0.107, 0.107, 0.000)


def test_market_and_system_operator_fees_are_the_published_ones() -> None:
    assert OMIE_FEE_EUR_MWH == 0.04070  # Orden TED/1524/2025, apartado octavo
    assert OS_FEE_EUR_MWH == 0.20332  # Resolución CNMC de 23-dic-2025
    assert regulated.OS_FIXED_FEE_EUR_MONTH == 200.0


@pytest.mark.parametrize(
    ("tariff", "local_fixed", "local_variable", "transport_fixed"),
    [
        (GasTariff.RL_1, 23.895429, 15.240, 1.518478),
        (GasTariff.RL_2, 64.732693, 13.172, 5.326102),
        (GasTariff.RL_3, 176.302005, 9.782, 13.012555),
        (GasTariff.RL_4, 430.268266, 12.887, 76.624284),
    ],
)
def test_gas_tolls_match_cnmc_resolution(tariff: GasTariff, local_fixed: float, local_variable: float, transport_fixed: float) -> None:
    table = GAS_REGULATED[tariff]
    assert (table.local_fixed_eur_year, table.local_variable_eur_mwh, table.transport_fixed_eur_year) == (
        local_fixed,
        local_variable,
        transport_fixed,
    )
    assert table.regasification_fixed_eur_year == 0.0  # Resolución CNMC de 2-jun-2026: valor cero en el año de gas 2027
    assert table.toll_fixed_eur_year == pytest.approx(local_fixed + transport_fixed)


def test_gas_consumption_bands_are_contiguous() -> None:
    tables = list(GAS_REGULATED.values())
    assert tables[0].min_mwh == 0.0 and tables[-1].max_mwh == 300.0
    assert all(low.max_mwh == high.min_mwh for low, high in zip(tables, tables[1:]))


def test_gas_imbalance_adjustment_is_three_percent_everywhere() -> None:
    assert GAS_IMBALANCE_SMALL_ADJUSTMENT == 0.03
    assert MibgasSettings().imbalance_small_adjustment == GAS_IMBALANCE_SMALL_ADJUSTMENT
    assert GAS_CNMC_FEE_RATIO == 0.00140


# --------------------------------------------------------------------------------------------------
# Unidades: de barras de central a contador
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tariff", list(PowerTariff))
def test_busbar_costs_are_raised_to_the_meter_with_losses(power: PowerPricingEngine, tariff: PowerTariff) -> None:
    quote = power.quote(PowerQuoteRequest(tariff=tariff, profile=LoadProfileType.INDUSTRIAL_FLAT, product=ProductStructure.INDEXED))
    table = POWER_REGULATED[tariff]
    index = power.periods(tariff) - 1
    to_meter = 1.0 + np.asarray(table.losses)[index]
    lines = {c.code: c.value_eur_mwh for c in quote.build_up}
    capacity = float((np.asarray(table.capacity_payment)[index] * to_meter).mean())
    other = float(((OS_FEE_EUR_MWH + OMIE_FEE_EUR_MWH) * to_meter).mean()) + FNEE_EUR_MWH
    assert lines["CAPACITY"] == pytest.approx(capacity, abs=0.0051)
    assert lines["OTHER_REGULATED"] == pytest.approx(other, abs=0.0051)
    assert lines["CAPACITY"] > float(np.asarray(table.capacity_payment)[index].mean()) - 0.0051  # contador >= barras
    # Por periodo: valle sin pagos por capacidad; en punta, el precio publicado elevado con sus pérdidas.
    periods = {p.period: p.components for p in quote.period_prices}
    assert periods[f"P{table.n_periods}"]["CAPACITY"] == 0.0
    assert periods["P1"]["CAPACITY"] == pytest.approx(table.capacity_payment[0] * (1.0 + table.losses[0]), abs=0.0051)


# --------------------------------------------------------------------------------------------------
# Registro de procedencia
# --------------------------------------------------------------------------------------------------


def test_register_classifies_every_parameter() -> None:
    keys = [p.key for p in REGULATORY_PARAMETERS]
    assert len(keys) == len(set(keys))
    for parameter in REGULATORY_PARAMETERS:
        assert parameter.commodity in {"POWER", "GAS"} and parameter.source and parameter.value and parameter.unit
        if parameter.status is ParameterStatus.CONFIGURABLE:
            assert parameter.env_var, f"{parameter.key} es configurable y no declara variable de entorno"
        if parameter.status is ParameterStatus.OFFICIAL_REFERENCE:
            assert parameter.note, f"{parameter.key} debe explicar por qué es solo una referencia"
    statuses = {p.key: p.status for p in REGULATORY_PARAMETERS}
    assert statuses["power.capacity_payment"] is ParameterStatus.OFFICIAL
    assert statuses["power.fnee"] is ParameterStatus.OFFICIAL_REFERENCE
    assert statuses["gas.charges_fixed"] is ParameterStatus.OFFICIAL_REFERENCE
    assert statuses["gas.shrinkage"] is ParameterStatus.CONFIGURABLE


def test_register_is_serialisable_and_shows_live_values() -> None:
    rows = {row["key"]: row for row in regulatory_register()}
    assert rows["power.omie_fee"]["value"] == "0.04070" and rows["power.omie_fee"]["status"] == "OFICIAL"
    assert rows["gas.local_variable"]["value"] == "15.240 / 13.172 / 9.782 / 12.887"
    assert "2.0TD: 0.800 / 0.133 / 0.000" in rows["power.capacity_payment"]["value"]
    assert all(isinstance(v, (str, type(None))) for row in rows.values() for v in row.values())


def test_configurable_parameters_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICING_TEST_VALUE", "3,25")  # coma decimal española admitida
    assert regulated._env_float("PRICING_TEST_VALUE", 1.0) == 3.25
    monkeypatch.setenv("PRICING_TEST_VALUE", "")
    assert regulated._env_float("PRICING_TEST_VALUE", 1.0) == 1.0
    monkeypatch.setenv("PRICING_TEST_VALUE", "abc")
    with pytest.raises(ValueError, match="no es un número válido"):
        regulated._env_float("PRICING_TEST_VALUE", 1.0)
    monkeypatch.setenv("PRICING_TEST_VALUE", "-1")
    with pytest.raises(ValueError, match="fuera del rango"):
        regulated._env_float("PRICING_TEST_VALUE", 1.0)
