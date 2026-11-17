"""Tests del motor de profiling y tarificación (electricidad y gas)."""

from __future__ import annotations

from datetime import date
from itertools import product as cartesian
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings
from app.core.pricing_engine import (
    GAS_REGULATED,
    HALF_NORMAL_MEAN,
    POWER_REGULATED,
    GasPricingEngine,
    PowerPricingEngine,
    PricingError,
    assign_periods,
    build_gas_profile,
    build_load_profile,
    to_cents,
)
from app.core.regulated import GAS_CNMC_FEE_RATIO, GAS_FNEE_EUR_MWH, GAS_GTS_QUOTA_RATIO
from app.models.tariffs import (
    ComponentCategory,
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
from app.pipelines.market_ingestion import MarketIngestionPipeline, SyntheticMarket, SyntheticMarketGenerator

STANDARD_PROFILES = [p for p in LoadProfileType if p is not LoadProfileType.CUSTOM]


@pytest.fixture(scope="module")
def market() -> SyntheticMarket:
    return SyntheticMarketGenerator(
        year=2025, seed=42, tz="Europe/Madrid", reinforced_operation_start=date(2025, 4, 28), small_adjustment=0.03
    ).generate()


@pytest.fixture(scope="module")
def power(market: SyntheticMarket) -> PowerPricingEngine:
    return PowerPricingEngine(market.power)


@pytest.fixture(scope="module")
def gas(market: SyntheticMarket) -> GasPricingEngine:
    return GasPricingEngine(market.gas)


def period_at(engine: PowerPricingEngine, tariff: PowerTariff, day: str, hour: int) -> int:
    calendar = engine.market
    mask = (calendar["date_local"] == date.fromisoformat(day)) & (calendar["hour_local"] == hour)
    return int(engine.periods(tariff)[mask.to_numpy()][0])


# --------------------------------------------------------------------------------------------------
# Periodos tarifarios (Circular CNMC 3/2020)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "hour", "expected"),
    [
        ("2025-01-15", 10, 1),  # temporada alta, punta
        ("2025-01-15", 8, 2),  # temporada alta, llano
        ("2025-03-12", 19, 2),  # media-alta, punta
        ("2025-03-12", 15, 3),  # media-alta, llano
        ("2025-06-11", 10, 3),  # media, punta
        ("2025-06-11", 23, 4),  # media, llano
        ("2025-04-09", 10, 4),  # baja, punta
        ("2025-04-09", 15, 5),  # baja, llano
        ("2025-01-15", 3, 6),  # madrugada
        ("2025-01-18", 11, 6),  # sábado
        ("2025-12-25", 11, 6),  # festivo nacional en jueves
        ("2025-01-06", 19, 6),  # Reyes en lunes
    ],
)
def test_six_period_calendar(power: PowerPricingEngine, day: str, hour: int, expected: int) -> None:
    assert period_at(power, PowerTariff.TD_30, day, hour) == expected
    assert period_at(power, PowerTariff.TD_61, day, hour) == expected


def test_holidays_and_weekends_are_always_valley(power: PowerPricingEngine) -> None:
    calendar = power.market
    off_day = (calendar["is_weekend"] | calendar["is_tariff_holiday"]).to_numpy()
    assert calendar["is_tariff_holiday"].sum() == 9 * 24
    for tariff, valley in ((PowerTariff.TD_30, 6), (PowerTariff.TD_61, 6), (PowerTariff.TD_20, 3)):
        periods = power.periods(tariff)
        assert (periods[off_day] == valley).all()
        assert set(np.unique(periods)) == set(range(1, valley + 1))
    assert np.array_equal(power.periods(PowerTariff.TD_30), assign_periods(calendar, PowerTariff.TD_61))


def test_three_period_calendar(power: PowerPricingEngine) -> None:
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 11) == 1
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 20) == 1
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 9) == 2
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 15) == 2
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 23) == 2
    assert period_at(power, PowerTariff.TD_20, "2025-01-15", 3) == 3
    assert period_at(power, PowerTariff.TD_20, "2025-05-01", 12) == 3  # festivo


# --------------------------------------------------------------------------------------------------
# Profiling de carga
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", STANDARD_PROFILES)
def test_profiles_scale_to_annual_consumption(power: PowerPricingEngine, profile: LoadProfileType) -> None:
    load = build_load_profile(power.market, profile, 1234.5)
    assert len(load) == 8760 and (load >= 0).all()
    assert load.sum() == pytest.approx(1234.5, rel=1e-12)


def test_profile_shapes_match_their_archetype(power: PowerPricingEngine) -> None:
    hour = power.market["hour_local"].to_numpy()
    by_hour = {
        p: pd.Series(build_load_profile(power.market, p, 1000.0)).groupby(hour).mean() for p in STANDARD_PROFILES
    }
    assert by_hour[LoadProfileType.INDUSTRIAL_FLAT].std() == pytest.approx(0.0, abs=1e-12)
    assert by_hour[LoadProfileType.COMMERCIAL_DAY].loc[10:17].mean() > 3 * by_hour[LoadProfileType.COMMERCIAL_DAY].loc[0:5].mean()
    assert by_hour[LoadProfileType.RESIDENTIAL_EVENING].idxmax() in (20, 21)
    solar = by_hour[LoadProfileType.SOLAR_PROSUMER]
    assert solar.loc[12:15].mean() < 0.35 * solar.loc[20:22].mean()  # caída de demanda en horas centrales
    aligned = by_hour[LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL]
    assert aligned.loc[10:16].sum() / aligned.sum() > 0.80  # consumo intensivo en la ventana 10:00-17:00
    assert aligned.idxmax() in range(11, 16)
    assert aligned.loc[0:6].mean() < 0.10 * aligned.loc[11:15].mean()  # de noche solo quedan los auxiliares


def test_custom_profile_is_validated_and_scaled(power: PowerPricingEngine) -> None:
    custom = np.linspace(1.0, 2.0, 8760)
    load = build_load_profile(power.market, LoadProfileType.CUSTOM, 500.0, custom.tolist())
    assert load.sum() == pytest.approx(500.0) and load[-1] == pytest.approx(2 * load[0])
    with pytest.raises(PricingError, match="8760 valores"):
        build_load_profile(power.market, LoadProfileType.CUSTOM, 500.0, [1.0] * 100)
    with pytest.raises(PricingError, match="no negativa"):
        build_load_profile(power.market, LoadProfileType.CUSTOM, 500.0, [-1.0] * 8760)
    with pytest.raises(ValueError, match="custom_profile"):
        PowerQuoteRequest(profile=LoadProfileType.CUSTOM)
    quote = power.quote(PowerQuoteRequest(profile=LoadProfileType.CUSTOM, custom_profile=[1.0] * 8760))
    assert quote.kpis.shape_cost_eur_mwh == 0.0


# --------------------------------------------------------------------------------------------------
# Tarificación eléctrica
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("tariff", "structure", "profile"), list(cartesian(PowerTariff, ProductStructure, STANDARD_PROFILES)))
def test_build_up_reconciles_to_the_cent(
    power: PowerPricingEngine, tariff: PowerTariff, structure: ProductStructure, profile: LoadProfileType
) -> None:
    quote = power.quote(PowerQuoteRequest(tariff=tariff, product=structure, profile=profile, annual_consumption_mwh=750.0))
    assert sum(to_cents(c.value_eur_mwh) for c in quote.build_up) == to_cents(quote.kpis.final_price_eur_mwh)
    assert len(quote.period_prices) == POWER_REGULATED[tariff].n_periods
    for period in quote.period_prices:
        assert sum(to_cents(v) for v in period.components.values()) == to_cents(period.price_eur_mwh)
    assert sum(p.consumption_mwh for p in quote.period_prices) == pytest.approx(750.0, abs=1e-3)
    assert sum(p.hours for p in quote.period_prices) == 8760
    assert abs(quote.tou_reconciliation_diff_eur_mwh) <= 0.03  # solo redondeo de céntimos por línea
    assert quote.kpis.annual_energy_bill_eur == pytest.approx(quote.kpis.final_price_eur_mwh * 750.0, abs=0.01)


def test_shape_cost_isolates_profile_value(power: PowerPricingEngine) -> None:
    quotes = {p: power.quote(PowerQuoteRequest(profile=p)).kpis for p in STANDARD_PROFILES}
    flat, residential, solar = (
        quotes[LoadProfileType.INDUSTRIAL_FLAT],
        quotes[LoadProfileType.RESIDENTIAL_EVENING],
        quotes[LoadProfileType.SOLAR_PROSUMER],
    )
    assert flat.shape_cost_eur_mwh == 0.0 and flat.shape_factor == 1.0
    assert residential.shape_cost_eur_mwh > 0
    assert solar.shape_cost_eur_mwh > residential.shape_cost_eur_mwh  # el prosumidor compra fuera de las horas solares
    assert quotes[LoadProfileType.COMMERCIAL_DAY].shape_cost_eur_mwh < 0  # consumo concentrado en el valle solar
    for kpis in quotes.values():
        assert kpis.baseload_omie_eur_mwh == pytest.approx(round(power.baseload, 2))
        assert kpis.shape_cost_eur_mwh == pytest.approx(kpis.weighted_omie_eur_mwh - kpis.baseload_omie_eur_mwh, abs=0.011)


def test_solar_archetypes_follow_iberian_market_physics(power: PowerPricingEngine) -> None:
    """Caso de estudio ibérico: quién paga la curva de pato.

    El prosumidor autoconsume al mediodía y compra el residuo de la punta de tarde: su Shape Cost supera al del
    residencial. La carga industrial que sigue al sol compra en el valle fotovoltaico: su Shape Cost es negativo.
    """
    def shape(profile: LoadProfileType, tariff: PowerTariff = PowerTariff.TD_30) -> float:
        return power.quote(PowerQuoteRequest(profile=profile, tariff=tariff)).kpis.shape_cost_eur_mwh

    prosumer, residential = shape(LoadProfileType.SOLAR_PROSUMER), shape(LoadProfileType.RESIDENTIAL_EVENING)
    aligned, commercial = shape(LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL), shape(LoadProfileType.COMMERCIAL_DAY)
    assert prosumer > residential > 0
    assert aligned < 0
    assert aligned < commercial  # concentrar el consumo en 10-17 h captura más valle que un horario comercial
    # El Shape Cost depende del perfil y del mercado, no de la tarifa de acceso con la que se cotice.
    for tariff in PowerTariff:
        assert shape(LoadProfileType.SOLAR_PROSUMER, tariff) == prosumer
        assert shape(LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL, tariff) == aligned
    # Lo que decide el signo es el precio capturado: por debajo del baseload para la carga solar, por encima para el prosumidor.
    kpis = power.quote(PowerQuoteRequest(profile=LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL)).kpis
    assert kpis.weighted_omie_eur_mwh < kpis.baseload_omie_eur_mwh and kpis.shape_factor < 1.0
    assert power.quote(PowerQuoteRequest(profile=LoadProfileType.SOLAR_PROSUMER)).kpis.shape_factor > 1.0


def test_solar_aligned_load_is_cheaper_all_in_than_the_same_volume_flat(power: PowerPricingEngine) -> None:
    request = PowerQuoteRequest(tariff=PowerTariff.TD_61, profile=LoadProfileType.SOLAR_ALIGNED_INDUSTRIAL, product=ProductStructure.INDEXED)
    aligned = power.quote(request).kpis
    flat = power.quote(request.model_copy(update={"profile": LoadProfileType.INDUSTRIAL_FLAT})).kpis
    assert aligned.weighted_omie_eur_mwh < flat.weighted_omie_eur_mwh
    assert aligned.shape_cost_eur_mwh < flat.shape_cost_eur_mwh == 0.0


def test_residential_evening_costs_more_than_daytime_solar_consumption(power: PowerPricingEngine) -> None:
    """Perfil nocturno vs perfil alineado con la producción solar (curva propia centrada en las 14 h)."""
    hour = power.market["hour_local"].to_numpy(dtype=float)
    solar_aligned = np.exp(-0.5 * ((hour - 13.5) / 2.5) ** 2).tolist()
    evening = power.quote(PowerQuoteRequest(profile=LoadProfileType.RESIDENTIAL_EVENING)).kpis
    daytime = power.quote(PowerQuoteRequest(profile=LoadProfileType.CUSTOM, custom_profile=solar_aligned)).kpis
    assert evening.shape_cost_eur_mwh > 0 > daytime.shape_cost_eur_mwh
    assert evening.shape_cost_eur_mwh - daytime.shape_cost_eur_mwh > 20.0


def test_imbalance_cost_parametric_and_monte_carlo(power: PowerPricingEngine) -> None:
    base = PowerQuoteRequest(profile=LoadProfileType.INDUSTRIAL_FLAT, forecast_error_pct=5.0)
    k5 = power.quote(base).kpis
    k10 = power.quote(base.model_copy(update={"forecast_error_pct": 10.0})).kpis
    k0 = power.quote(base.model_copy(update={"forecast_error_pct": 0.0})).kpis
    spread = (power.imbalance_down - power.imbalance_up).mean()
    assert k5.imbalance_cost_eur_mwh == pytest.approx(0.05 * HALF_NORMAL_MEAN * spread, abs=0.006)
    assert k10.imbalance_cost_eur_mwh == pytest.approx(2 * k5.imbalance_cost_eur_mwh, abs=0.011)
    assert k0.imbalance_cost_eur_mwh == 0.0 and k5.imbalance_cost_p95_eur_mwh is None
    mc = power.quote(base.model_copy(update={"imbalance_method": ImbalanceMethod.MONTE_CARLO, "monte_carlo_paths": 300})).kpis
    assert mc.imbalance_cost_eur_mwh == pytest.approx(k5.imbalance_cost_eur_mwh, rel=0.05)
    assert mc.imbalance_cost_p95_eur_mwh is not None and mc.imbalance_cost_p95_eur_mwh >= mc.imbalance_cost_eur_mwh


def test_asymmetric_imbalance_penalises_evening_profiles(power: PowerPricingEngine) -> None:
    flat = power.quote(PowerQuoteRequest(profile=LoadProfileType.INDUSTRIAL_FLAT)).kpis.imbalance_cost_eur_mwh
    evening = power.quote(PowerQuoteRequest(profile=LoadProfileType.RESIDENTIAL_EVENING)).kpis.imbalance_cost_eur_mwh
    assert evening > flat


def test_product_structures(power: PowerPricingEngine) -> None:
    base = PowerQuoteRequest(profile=LoadProfileType.RESIDENTIAL_EVENING, margin_eur_mwh=5.0)
    fixed = power.quote(base)
    tou = power.quote(base.model_copy(update={"product": ProductStructure.FIXED_TOU}))
    indexed = power.quote(base.model_copy(update={"product": ProductStructure.INDEXED}))
    codes = {c.code: c for c in fixed.build_up}
    assert fixed.kpis.risk_premium_eur_mwh > 0 and not codes["OMIE_BASELOAD"].pass_through
    assert codes["TOLLS"].pass_through and codes["MARGIN"].value_eur_mwh == 5.0
    assert tou.kpis.final_price_eur_mwh == fixed.kpis.final_price_eur_mwh
    prices = [p.price_eur_mwh for p in tou.period_prices]
    assert prices[0] > prices[-1] and len(set(prices)) == 6  # P1 más caro que P6
    indexed_codes = {c.code: c for c in indexed.build_up}
    assert "RISK_PREMIUM" not in indexed_codes and indexed.kpis.risk_premium_eur_mwh == 0.0
    assert all(indexed_codes[c].pass_through for c in ("OMIE_BASELOAD", "SHAPE", "SSAA", "IMBALANCE", "TOLLS"))
    assert indexed_codes["MARGIN"].label == "Fee de gestión" and not indexed_codes["MARGIN"].pass_through
    assert to_cents(fixed.kpis.final_price_eur_mwh) - to_cents(indexed.kpis.final_price_eur_mwh) == to_cents(
        fixed.kpis.risk_premium_eur_mwh
    )


def test_cap_option_premium(power: PowerPricingEngine) -> None:
    base = PowerQuoteRequest(product=ProductStructure.FIXED_WITH_CAP, profile=LoadProfileType.RESIDENTIAL_EVENING)
    cap90 = power.quote(base)
    cap70 = power.quote(base.model_copy(update={"cap_strike_eur_mwh": 70.0}))
    cap_far = power.quote(base.model_copy(update={"cap_strike_eur_mwh": 900.0}))
    indexed = power.quote(base.model_copy(update={"product": ProductStructure.INDEXED}))
    assert cap90.kpis.cap_option_premium_eur_mwh is not None and cap90.kpis.cap_option_premium_eur_mwh > 0
    assert cap70.kpis.cap_option_premium_eur_mwh > cap90.kpis.cap_option_premium_eur_mwh
    assert cap70.kpis.cap_hours_in_the_money > cap90.kpis.cap_hours_in_the_money > 0
    assert cap_far.kpis.cap_option_premium_eur_mwh == 0.0 and cap_far.kpis.cap_hours_in_the_money == 0
    lines = {c.code: c.value_eur_mwh for c in cap90.build_up}
    load = build_load_profile(power.market, LoadProfileType.RESIDENTIAL_EVENING, 500.0)
    historical_value = float(np.maximum(power.spot - 90.0, 0.0) @ load / load.sum())
    assert lines["CAP_PAYOFF"] == pytest.approx(-historical_value, abs=0.006)
    assert lines["CAP_PREMIUM"] == pytest.approx(1.10 * historical_value, abs=0.006)
    # El cliente paga el indexado más el recargo de riesgo de la opción (10 % de su valor histórico).
    assert cap90.kpis.final_price_eur_mwh - indexed.kpis.final_price_eur_mwh == pytest.approx(0.10 * historical_value, abs=0.011)
    assert any("por debajo del baseload" in w for w in power.quote(base.model_copy(update={"cap_strike_eur_mwh": 40.0})).warnings)


def test_regulated_costs_follow_published_table(power: PowerPricingEngine) -> None:
    quote = power.quote(PowerQuoteRequest(tariff=PowerTariff.TD_20, profile=LoadProfileType.RESIDENTIAL_EVENING, contracted_power_kw=4.6))
    p1, p3 = quote.period_prices[0].components, quote.period_prices[2].components
    assert (p1["TOLLS"], p1["CHARGES"]) == (33.26, 64.29)
    assert (p3["TOLLS"], p3["CHARGES"]) == (0.08, 3.22)
    assert quote.kpis.power_term_eur_year == pytest.approx(4.6 * (23.324952 + 0.443770 + 4.379461 + 0.281653), abs=0.01)
    regulated = sum(to_cents(c.value_eur_mwh) for c in quote.build_up if c.category is ComponentCategory.REGULATED)
    assert regulated == to_cents(quote.kpis.regulated_cost_eur_mwh)
    assert quote.hourly_profile[21].client_load_index > 100 > quote.hourly_profile[4].client_load_index
    assert len(quote.hourly_profile) == 24 and np.mean([h.omie_price_index for h in quote.hourly_profile]) == pytest.approx(100, abs=0.1)


def test_engine_rejects_incomplete_market(market: SyntheticMarket) -> None:
    with pytest.raises(PricingError, match="spot_price_eur_mwh"):
        PowerPricingEngine(market.power.drop(columns=["spot_price_eur_mwh"]))
    with pytest.raises(PricingError, match="hdd_18c"):
        GasPricingEngine(market.gas.drop(columns=["hdd_18c"]))


def test_engines_load_from_warehouse(tmp_path: Path) -> None:
    MarketIngestionPipeline(Settings(mock_data=True, warehouse_dir=tmp_path, random_seed=3)).run()
    power_quote = PowerPricingEngine.from_warehouse(tmp_path).quote(PowerQuoteRequest())
    gas_quote = GasPricingEngine.from_warehouse(tmp_path).quote(GasQuoteRequest())
    assert power_quote.data_source == "SYNTHETIC" and power_quote.market_year == 2025
    assert gas_quote.kpis.final_price_eur_mwh > 0 and any("sintético" in w for w in gas_quote.warnings)


# --------------------------------------------------------------------------------------------------
# Gas natural
# --------------------------------------------------------------------------------------------------

GAS_VOLUMES = {GasTariff.RL_1: 3.0, GasTariff.RL_2: 9.0, GasTariff.RL_3: 30.0, GasTariff.RL_4: 150.0}


@pytest.mark.parametrize(("tariff", "profile", "structure"), list(cartesian(GasTariff, GasProfileType, GasProduct)))
def test_gas_quote_is_complete_and_reconciles(
    gas: GasPricingEngine, tariff: GasTariff, profile: GasProfileType, structure: GasProduct
) -> None:
    volume = GAS_VOLUMES[tariff]
    quote = gas.quote(GasQuoteRequest(tariff=tariff, profile=profile, product=structure, annual_consumption_mwh=volume))
    lines = {c.code: c.value_eur_mwh for c in quote.build_up}
    assert sum(to_cents(v) for v in lines.values()) == to_cents(quote.kpis.final_price_eur_mwh)
    table = GAS_REGULATED[tariff]
    uplift = 1.0 + GAS_CNMC_FEE_RATIO + GAS_GTS_QUOTA_RATIO  # tasa CNMC + cuota GTS sobre peajes
    assert lines["TOLL_VARIABLE"] == pytest.approx(table.local_variable_eur_mwh * uplift, abs=0.006)
    assert lines["TOLL_FIXED"] == pytest.approx(table.toll_fixed_eur_year * uplift / volume, abs=0.006)
    charges = table.charge_fixed_eur_year * (1.0 + GAS_CNMC_FEE_RATIO) / volume + GAS_FNEE_EUR_MWH
    assert lines["OTHER_REGULATED"] == pytest.approx(charges, abs=0.006)
    assert lines["IMBALANCE"] > 0 and ("RISK_PREMIUM" in lines) == (structure is GasProduct.FIXED)
    assert len(quote.monthly) == 12
    assert sum(m.consumption_mwh for m in quote.monthly) == pytest.approx(volume, abs=1e-3)
    assert quote.kpis.annual_bill_eur == pytest.approx(quote.kpis.final_price_eur_mwh * volume, abs=0.01)
    assert not any("fuera de la banda" in w for w in quote.warnings)


def test_gas_seasonal_shape_cost_follows_heating_demand(gas: GasPricingEngine) -> None:
    kpis = {p: gas.quote(GasQuoteRequest(profile=p)).kpis for p in GasProfileType}
    heating, commercial, process = (
        kpis[GasProfileType.RESIDENTIAL_HEATING],
        kpis[GasProfileType.COMMERCIAL],
        kpis[GasProfileType.INDUSTRIAL_PROCESS],
    )
    assert heating.seasonal_shape_cost_eur_mwh > commercial.seasonal_shape_cost_eur_mwh > process.seasonal_shape_cost_eur_mwh >= 0
    assert heating.winter_consumption_share_pct > 80 > process.winter_consumption_share_pct
    assert heating.seasonal_shape_cost_eur_mwh == pytest.approx(heating.weighted_pvb_eur_mwh - heating.baseload_pvb_eur_mwh, abs=0.011)
    load = build_gas_profile(gas.market, GasProfileType.RESIDENTIAL_HEATING, 9.0)
    assert load.sum() == pytest.approx(9.0) and load[gas.hdd.argmax()] == load.max()


def test_gas_volume_outside_tariff_band_is_flagged(gas: GasPricingEngine) -> None:
    quote = gas.quote(GasQuoteRequest(tariff=GasTariff.RL_1, annual_consumption_mwh=40.0))
    assert any("fuera de la banda de RL.1" in w for w in quote.warnings)
