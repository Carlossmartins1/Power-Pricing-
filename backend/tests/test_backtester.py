"""Tests del back-tester de cartera: generación, estrés, reconciliación del waterfall y alertas de repricing."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.backtester import UNFORECAST_VOLUME_SHARE, PortfolioBacktester
from app.core.config import Settings
from app.core.pricing_engine import GAS_REGULATED, HALF_NORMAL_MEAN, PricingError
from app.models.portfolio import AlertLevel, BacktestRequest, Commodity, ContractProduct, StressScenario
from app.models.tariffs import GasTariff
from app.pipelines.market_ingestion import MarketIngestionPipeline, SyntheticMarket, SyntheticMarketGenerator

SEED = 7
EFFECTS = ["volume_effect_eur", "shape_effect_eur", "wholesale_hedging_effect_eur", "imbalance_effect_eur"]
STRESSED = StressScenario(
    volume_shock_pct=15, shape_shift_pct=10, imbalance_multiplier=2.0, unhedged_ratio=0.30, wholesale_price_shock_pct=10
)


@pytest.fixture(scope="module")
def market() -> SyntheticMarket:
    return SyntheticMarketGenerator(
        year=2025, seed=42, tz="Europe/Madrid", reinforced_operation_start=date(2025, 4, 28), small_adjustment=0.03
    ).generate()


@pytest.fixture(scope="module")
def backtester(market: SyntheticMarket) -> PortfolioBacktester:
    return PortfolioBacktester(market.power, market.gas)


@pytest.fixture(scope="module")
def portfolio() -> pd.DataFrame:
    return PortfolioBacktester.generate_portfolio(500, SEED)


def cents(value: float) -> int:
    return int(round(value * 100))


def test_portfolio_has_500_realistic_contracts(portfolio: pd.DataFrame) -> None:
    assert len(portfolio) == 500 and portfolio["contract_id"].is_unique
    assert set(portfolio["tariff"]) == {"2.0TD", "3.0TD", "6.1TD", "RL.1", "RL.2", "RL.3"}
    assert set(portfolio["signed_months_ago"]) == {6, 12}
    assert set(portfolio["product"]) == {"FIXED", "INDEXED"}
    counts = portfolio["tariff"].value_counts()
    assert counts["2.0TD"] > counts["3.0TD"] > counts["6.1TD"]
    for tariff in (GasTariff.RL_1, GasTariff.RL_2, GasTariff.RL_3):
        volumes = portfolio.loc[portfolio["tariff"] == tariff.value, "annual_mwh"]
        assert volumes.gt(GAS_REGULATED[tariff].min_mwh).all() and volumes.le(GAS_REGULATED[tariff].max_mwh).all()
    pd.testing.assert_frame_equal(portfolio, PortfolioBacktester.generate_portfolio(500, SEED))
    assert not portfolio["annual_mwh"].equals(PortfolioBacktester.generate_portfolio(500, SEED + 1)["annual_mwh"])


@pytest.mark.parametrize("stress", [StressScenario(), STRESSED, StressScenario(volume_shock_pct=-20, shape_shift_pct=-15, unhedged_ratio=1.0)])
def test_waterfall_reconciles_exactly(backtester: PortfolioBacktester, stress: StressScenario) -> None:
    response = backtester.run(BacktestRequest(stress=stress, seed=SEED))
    steps = {s.key: s for s in response.waterfall}
    assert [s.key for s in response.waterfall] == ["ex_ante", *EFFECTS, "ex_post"]
    effect_cents = sum(cents(steps[k].value_eur) for k in EFFECTS)
    assert cents(steps["ex_ante"].value_eur) + effect_cents == cents(steps["ex_post"].value_eur)
    assert cents(response.kpis.margin_delta_eur) == effect_cents
    assert cents(response.kpis.ex_post_margin_eur) - cents(response.kpis.ex_ante_margin_eur) == effect_cents
    running = steps["ex_ante"].end_eur
    for key in EFFECTS:
        assert steps[key].start_eur == pytest.approx(running, abs=1e-9)
        assert steps[key].end_eur == pytest.approx(running + steps[key].value_eur, abs=0.005)
        running = steps[key].end_eur
    assert running == pytest.approx(steps["ex_post"].value_eur, abs=0.005)
    assert response.attribution.model_dump() == {k: steps[k].value_eur for k in EFFECTS}
    assert abs(response.reconciliation_error_eur) <= 0.03  # solo redondeo de los cinco sumandos


def test_contract_level_attribution_is_exact(backtester: PortfolioBacktester, portfolio: pd.DataFrame) -> None:
    result = backtester.evaluate(portfolio, STRESSED, SEED)
    explained = result["ex_ante_margin_eur"] + result[EFFECTS].sum(axis=1)
    np.testing.assert_allclose(explained, result["ex_post_margin_eur"], rtol=0, atol=1e-9)
    indexed = result["product"] == ContractProduct.INDEXED.value
    assert indexed.sum() > 20
    assert (result.loc[indexed, ["shape_effect_eur", "wholesale_hedging_effect_eur"]] == 0).all().all()
    assert (result.loc[~indexed, "shape_effect_eur"].abs() > 0).all()


@pytest.mark.parametrize("commodity", [Commodity.POWER, Commodity.GAS])
def test_ex_post_margin_matches_first_principles_cash_flows(
    backtester: PortfolioBacktester, portfolio: pd.DataFrame, commodity: Commodity
) -> None:
    """Recalcula ingresos y costes hora a hora (o día a día) para un contrato fijo y compara con el motor."""
    result = backtester.evaluate(portfolio, STRESSED, SEED)
    candidates = portfolio[(portfolio["commodity"] == commodity.value) & (portfolio["product"] == "FIXED")]
    for idx in candidates.index[:5]:
        contract, outcome = portfolio.loc[idx], result.loc[idx]
        curves = backtester.contract_curves(commodity, contract["profile"], int(contract["signed_months_ago"]), STRESSED, SEED)
        v0, v1 = outcome["contracted_mwh"], outcome["realized_mwh"]
        assert v0 == pytest.approx(contract["annual_mwh"] * curves.window_share)
        hedge = (1 - STRESSED.unhedged_ratio) * v0 * curves.contracted_shape
        consumption = v1 * curves.realized_shape
        sigma = contract["forecast_error_pct"] / 100 * contract["error_factor"] + UNFORECAST_VOLUME_SHARE * abs(v1 / v0 - 1)
        imbalance_cost = float(consumption @ curves.imbalance_spread) * sigma * HALF_NORMAL_MEAN
        procurement = float(hedge @ curves.forward + (consumption - hedge) @ curves.realized + consumption @ curves.ancillary)
        margin = outcome["contract_price_eur_mwh"] * v1 - procurement - imbalance_cost
        assert margin == pytest.approx(outcome["ex_post_margin_eur"], rel=1e-9, abs=1e-6)
        assert outcome["ex_ante_margin_eur"] > 0


def test_delivery_window_depends_on_signing_cohort(backtester: PortfolioBacktester, portfolio: pd.DataFrame) -> None:
    result = backtester.evaluate(portfolio, StressScenario(), SEED)
    share = result["contracted_mwh"] / portfolio["annual_mwh"]
    twelve = portfolio["signed_months_ago"] == 12
    assert np.allclose(share[twelve], 1.0)
    assert share[~twelve].between(0.2, 0.75).all()
    flat_power_six = (~twelve) & (portfolio["profile"] == "INDUSTRIAL_FLAT")
    assert np.allclose(share[flat_power_six], 184 * 24 / 8760, atol=0.001)  # julio-diciembre
    forward_12 = backtester.forward_curve(Commodity.POWER, 12, SEED)
    forward_6 = backtester.forward_curve(Commodity.POWER, 6, SEED)
    assert not np.allclose(forward_12, forward_6) and (forward_12 != backtester.realized_curve(Commodity.POWER, StressScenario())).any()


def test_stress_parameters_move_their_own_effect(backtester: PortfolioBacktester) -> None:
    def attribution(**stress: float) -> dict[str, float]:
        return backtester.run(BacktestRequest(seed=SEED, stress=StressScenario(**stress))).attribution.model_dump()

    base = attribution()
    assert base["shape_effect_eur"] == 0.0
    assert attribution(imbalance_multiplier=3.0)["imbalance_effect_eur"] < base["imbalance_effect_eur"] < attribution(imbalance_multiplier=0.0)["imbalance_effect_eur"]
    assert attribution(shape_shift_pct=20)["shape_effect_eur"] < 0 < attribution(shape_shift_pct=-20)["shape_effect_eur"]
    assert attribution(volume_shock_pct=15)["volume_effect_eur"] > base["volume_effect_eur"] > attribution(volume_shock_pct=-15)["volume_effect_eur"]
    hedged = attribution(wholesale_price_shock_pct=25, unhedged_ratio=0.0)["wholesale_hedging_effect_eur"]
    half_open = attribution(wholesale_price_shock_pct=25, unhedged_ratio=0.5)["wholesale_hedging_effect_eur"]
    fully_open = attribution(wholesale_price_shock_pct=25, unhedged_ratio=1.0)["wholesale_hedging_effect_eur"]
    assert fully_open < half_open < hedged
    assert attribution(wholesale_price_shock_pct=-25, unhedged_ratio=1.0)["wholesale_hedging_effect_eur"] > 0
    # El multiplicador de desvíos no contamina los demás efectos.
    stressed = attribution(imbalance_multiplier=3.0)
    assert all(stressed[k] == base[k] for k in EFFECTS if k != "imbalance_effect_eur")


def test_gas_cold_snap_raises_volume_and_shifts_shape(backtester: PortfolioBacktester, portfolio: pd.DataFrame) -> None:
    calm = backtester.evaluate(portfolio, StressScenario(), SEED)
    cold = backtester.evaluate(portfolio, StressScenario(volume_shock_pct=20), SEED)
    heating = (portfolio["profile"] == "RESIDENTIAL_HEATING") & (portfolio["product"] == "FIXED")
    process = portfolio["profile"] == "INDUSTRIAL_PROCESS"
    uplift = cold["realized_mwh"] / calm["realized_mwh"] - 1
    assert uplift[heating].mean() > 0.20 > uplift[process].mean() > 0
    assert (cold.loc[heating, "shape_effect_eur"] < 0).all()  # más consumo en los días fríos y caros


def test_reprice_alerts(backtester: PortfolioBacktester) -> None:
    request = BacktestRequest(seed=SEED, stress=STRESSED, margin_threshold_eur_mwh=2.0, max_alerts=40)
    response = backtester.run(request)
    alerts = response.at_risk_contracts
    assert 0 < len(alerts) <= 40 and response.kpis.contracts_at_risk >= len(alerts)
    assert all(a.alert is AlertLevel.REPRICE_REQUIRED and a.realized_unit_margin_eur_mwh < 2.0 for a in alerts)
    margins = [a.realized_unit_margin_eur_mwh for a in alerts]
    assert margins == sorted(margins) and margins[0] < 0
    full = backtester.evaluate(backtester.generate_portfolio(500, SEED), STRESSED, SEED)
    assert response.kpis.contracts_at_risk == int((full["realized_unit_margin_eur_mwh"] < 2.0).sum())
    calm = backtester.run(BacktestRequest(seed=SEED))
    assert calm.kpis.contracts_at_risk < response.kpis.contracts_at_risk
    stricter = backtester.run(BacktestRequest(seed=SEED, margin_threshold_eur_mwh=4.0))
    assert stricter.kpis.contracts_at_risk > calm.kpis.contracts_at_risk
    assert 0 < calm.kpis.at_risk_volume_share_pct < 100


def test_segments_and_kpis_are_consistent(backtester: PortfolioBacktester) -> None:
    response = backtester.run(BacktestRequest(seed=SEED, stress=STRESSED))
    kpis = response.kpis
    assert sum(s.contracts for s in response.segments) == kpis.contracts == 500
    assert sum(s.contracted_mwh for s in response.segments) == pytest.approx(kpis.contracted_mwh, abs=0.05)
    assert sum(s.ex_post_margin_eur for s in response.segments) == pytest.approx(kpis.ex_post_margin_eur, abs=0.1)
    assert sum(s.contracts_at_risk for s in response.segments) == kpis.contracts_at_risk
    assert kpis.ex_post_unit_margin_eur_mwh < kpis.ex_ante_unit_margin_eur_mwh
    assert kpis.open_position_mwh > 0 and response.market_year == 2025 and len(response.assumptions) >= 4
    assert {s.commodity for s in response.segments} == {Commodity.POWER, Commodity.GAS}


def test_backtester_loads_from_warehouse_and_validates_inputs(tmp_path: Path, market: SyntheticMarket) -> None:
    MarketIngestionPipeline(Settings(mock_data=True, warehouse_dir=tmp_path, random_seed=5)).run()
    response = PortfolioBacktester.from_warehouse(tmp_path).run(BacktestRequest(n_contracts=60))
    assert response.kpis.contracts == 60 and response.data_source == "SYNTHETIC"
    with pytest.raises(PricingError, match="gas_market"):
        PortfolioBacktester(market.power, market.gas.drop(columns=["hdd_18c"]))
    with pytest.raises(PricingError, match="Cohorte"):
        PortfolioBacktester(market.power, market.gas).contract_curves(Commodity.POWER, "INDUSTRIAL_FLAT", 9, StressScenario(), 1)
