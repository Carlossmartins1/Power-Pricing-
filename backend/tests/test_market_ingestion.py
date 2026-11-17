"""Tests de la capa de ingesta Multi-Commodity (modo sintético, conectores reales simulados y persistencia)."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
import requests

from app.core.config import EsiosSettings, MibgasSettings, Settings
from app.pipelines.market_ingestion import (
    GAS_ARROW_SCHEMA,
    GAS_COLUMNS,
    MANIFEST_FILENAME,
    POWER_COLUMNS,
    DataSource,
    DataValidationError,
    EsiosClient,
    IngestionError,
    MarketIngestionPipeline,
    MibgasClient,
    SyntheticMarket,
    SyntheticMarketGenerator,
    cross_commodity_correlation,
    easter_sunday,
    gas_imbalance_prices_nc_bal,
    hourly_utc_grid,
    load_gas_market,
    load_power_market,
    power_arrow_schema,
    spanish_tariff_holidays,
    validate_gas_frame,
    validate_power_frame,
)

YEAR = 2025
TZ = "Europe/Madrid"


# --------------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def market() -> SyntheticMarket:
    return SyntheticMarketGenerator(
        year=YEAR, seed=42, tz=TZ, reinforced_operation_start=date(2025, 4, 28), small_adjustment=0.03
    ).generate()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "mock_data": True,
        "strict": False,
        "market_year": YEAR,
        "random_seed": 7,
        "timezone": TZ,
        "warehouse_dir": tmp_path,
        "esios": EsiosSettings(api_token=None),
        "mibgas": MibgasSettings(),
    }
    base.update(overrides)
    return Settings(**base)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        return self._payload


class FakeEsiosSession(requests.Session):
    """Simula `/indicators/{id}` con granularidad cuartohoraria (MTU15) y dos geografías."""

    def __init__(self, missing_hours: int = 0) -> None:
        super().__init__()
        self.calls = 0
        self.missing_hours = missing_hours

    def get(self, url: str, params: Any = None, timeout: float | None = None, **_: Any) -> FakeResponse:  # type: ignore[override]
        self.calls += 1
        indicator_id = int(url.rstrip("/").split("/")[-1])
        query = dict(params)
        start = pd.Timestamp(query["start_date"])
        end = pd.Timestamp(query["end_date"])
        quarters = pd.date_range(start.floor("h"), end, freq="15min", tz="UTC")
        if self.missing_hours:
            quarters = quarters[quarters >= quarters[0] + pd.Timedelta(hours=self.missing_hours)]
        base = 50.0 + indicator_id / 100.0
        values: list[dict[str, Any]] = []
        for ts in quarters:
            hourly_value = base + ts.hour  # constante dentro de la hora
            iso = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            values.append({"value": hourly_value, "datetime_utc": iso, "geo_id": 3, "geo_name": "España"})
            values.append({"value": -999.0, "datetime_utc": iso, "geo_id": 1, "geo_name": "Portugal"})
        return FakeResponse(200, {"indicator": {"id": indicator_id, "values": values}})


def build_mibgas_workbook(year: int) -> bytes:
    """XLSX con la estructura de MIBGAS: filas de título, cabecera desplazada, PVB/VTP y varios productos."""
    rows: list[list[Any]] = []
    for day in pd.date_range(f"{year - 1}-12-31", f"{year}-12-30", freq="D"):
        if day.day_of_week >= 5:  # sin sesión en fin de semana
            continue
        delivery = day + pd.Timedelta(days=1)
        pvb = round(30.0 + delivery.day_of_year / 50.0, 2)
        rows.append([day.strftime("%d/%m/%Y"), "GDAES_D+1", "PVB", delivery.strftime("%d/%m/%Y"), delivery.strftime("%d/%m/%Y"), pvb, 1200])
        rows.append([day.strftime("%d/%m/%Y"), "GDAES_D+1", "VTP", delivery.strftime("%d/%m/%Y"), delivery.strftime("%d/%m/%Y"), pvb + 5, 300])
        rows.append([day.strftime("%d/%m/%Y"), "GMAES_M+1", "PVB", delivery.strftime("%d/%m/%Y"), delivery.strftime("%d/%m/%Y"), 99.0, 50])
    header = ["Trading day", "Product", "Area", "First Day Delivery", "Last Day Delivery", "Reference Price [EUR/MWh]", "Volume [MWh]"]
    sheet = pd.DataFrame([["MIBGAS Trading Data"] + [None] * 6, [None] * 7, header, *rows])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame([["Cover sheet"]]).to_excel(writer, sheet_name="Info", header=False, index=False)
        sheet.to_excel(writer, sheet_name="Trading Data PVB&VTP", header=False, index=False)
    return buffer.getvalue()


class FakeMibgasSession(requests.Session):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self._content = content

    def get(self, url: str, timeout: float | None = None, **_: Any) -> FakeResponse:  # type: ignore[override]
        return FakeResponse(200, content=self._content)


class FailingSession(requests.Session):
    def get(self, *_: Any, **__: Any) -> FakeResponse:  # type: ignore[override]
        raise requests.ConnectionError("network unreachable")


# --------------------------------------------------------------------------------------------------
# Calendario
# --------------------------------------------------------------------------------------------------


def test_calendar_grid_and_holidays() -> None:
    grid = hourly_utc_grid(YEAR, TZ)
    assert len(grid) == 8760
    assert grid[0] == pd.Timestamp("2024-12-31T23:00:00Z")
    local_counts = pd.Series(grid.tz_convert(TZ).date).value_counts()
    assert local_counts[date(2025, 3, 30)] == 23  # cambio a horario de verano
    assert local_counts[date(2025, 10, 26)] == 25  # cambio a horario de invierno
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)
    holidays = spanish_tariff_holidays(YEAR)
    assert date(2025, 12, 25) in holidays and date(2025, 4, 18) not in holidays


# --------------------------------------------------------------------------------------------------
# Modo sintético: integridad y realismo estadístico
# --------------------------------------------------------------------------------------------------


def test_synthetic_datasets_shape_and_quality(market: SyntheticMarket) -> None:
    power, gas = market.power, market.gas
    assert tuple(power.columns) == POWER_COLUMNS and len(power) == 8760
    assert tuple(gas.columns) == GAS_COLUMNS and len(gas) == 365
    assert not power.isna().any().any() and not gas.isna().any().any()
    assert validate_power_frame(power, YEAR, TZ).passed
    assert validate_gas_frame(gas, YEAR).passed


def test_duck_curve_and_evening_peak(market: SyntheticMarket) -> None:
    spring = market.power[market.power["month"].isin([4, 5])]
    by_hour = spring.groupby("hour_local")["spot_price_eur_mwh"].mean()
    solar_trough = by_hour.loc[13:16].mean()
    evening_peak = by_hour.loc[20:22].mean()
    night = by_hour.loc[2:5].mean()
    assert solar_trough < 15.0
    assert evening_peak > solar_trough + 50.0
    assert evening_peak > night  # pico nocturno-tarde por encima del valle de madrugada


def test_weekday_weekend_and_seasonality(market: SyntheticMarket) -> None:
    p = market.power
    workday = p[~p["is_weekend"] & ~p["is_tariff_holiday"]]["spot_price_eur_mwh"].mean()
    weekend = p[p["is_weekend"]]["spot_price_eur_mwh"].mean()
    assert workday > weekend + 5.0
    monthly = p.groupby("month")["spot_price_eur_mwh"].mean()
    assert monthly.loc[[1, 2, 12]].mean() > monthly.loc[[4, 5]].mean() + 20.0
    assert 45.0 < p["spot_price_eur_mwh"].mean() < 95.0
    assert (p["spot_price_eur_mwh"] <= 0).sum() > 100  # canibalización solar


def test_imbalance_prices_are_dual_and_asymmetric(market: SyntheticMarket) -> None:
    p = market.power
    spot = p["spot_price_eur_mwh"]
    assert (p["imbalance_price_up_eur_mwh"] <= spot).all()
    assert (p["imbalance_price_down_eur_mwh"] >= spot).all()
    short_premium = p["imbalance_price_down_eur_mwh"] - spot
    long_discount = spot - p["imbalance_price_up_eur_mwh"]
    evening_ramp = p["hour_local"].isin([18, 19, 20])
    morning_ramp = p["hour_local"].isin([8, 9, 10])
    flat_night = p["hour_local"].isin([1, 2, 3, 4])
    assert short_premium[evening_ramp].mean() > 1.4 * short_premium[flat_night].mean()
    assert long_discount[morning_ramp].mean() > 1.4 * long_discount[flat_night].mean()


def test_ancillary_services_reconcile_to_the_cent(market: SyntheticMarket) -> None:
    p = market.power
    components = (
        (p["ancillary_constraints_eur_mwh"] * 100).round().astype(int)
        + (p["ancillary_reserves_eur_mwh"] * 100).round().astype(int)
        + (p["ancillary_other_eur_mwh"] * 100).round().astype(int)
    )
    assert ((p["ancillary_services_cost_eur_mwh"] * 100).round().astype(int) == components).all()
    before = p[p["datetime_utc"] < pd.Timestamp("2025-04-27T22:00:00Z")]["ancillary_services_cost_eur_mwh"].mean()
    after = p[p["datetime_utc"] >= pd.Timestamp("2025-04-27T22:00:00Z")]["ancillary_services_cost_eur_mwh"].mean()
    assert after > before + 4.0  # operación reforzada post 28-abr-2025


def test_gas_seasonality_imbalance_and_power_correlation(market: SyntheticMarket) -> None:
    g = market.gas
    winter = g[g["gas_season"] == "WINTER"]["pvb_da_price_eur_mwh"].mean()
    summer = g[g["gas_season"] == "SUMMER"]["pvb_da_price_eur_mwh"].mean()
    assert winter > summer
    assert (g["imbalance_buy_price_eur_mwh"] >= g["pvb_da_price_eur_mwh"] * 1.03 - 0.01).all()
    assert (g["imbalance_sell_price_eur_mwh"] <= g["pvb_da_price_eur_mwh"] * 0.97 + 0.01).all()
    corr = cross_commodity_correlation(market.power, g)
    assert 0.3 < corr < 0.9  # correlación parcial (CCGT marginal solo en horas de residual alto)


def test_synthetic_generation_is_reproducible() -> None:
    kwargs = {"year": YEAR, "tz": TZ, "reinforced_operation_start": date(2025, 4, 28), "small_adjustment": 0.03}
    a = SyntheticMarketGenerator(seed=11, **kwargs).generate()
    b = SyntheticMarketGenerator(seed=11, **kwargs).generate()
    c = SyntheticMarketGenerator(seed=12, **kwargs).generate()
    pd.testing.assert_frame_equal(a.power, b.power)
    pd.testing.assert_frame_equal(a.gas, b.gas)
    assert not a.power["spot_price_eur_mwh"].equals(c.power["spot_price_eur_mwh"])


def test_gas_imbalance_rule_nc_bal() -> None:
    ref = np.array([40.0, 40.0, 40.0])
    buy, sell = gas_imbalance_prices_nc_bal(ref, 0.03, np.array([np.nan, 43.0, np.nan]), np.array([np.nan, np.nan, 37.0]))
    np.testing.assert_allclose(buy, [41.2, 43.0, 41.2])  # máx(compra del GTS, referencia x 1,03)
    np.testing.assert_allclose(sell, [38.8, 38.8, 37.0])  # mín(venta del GTS, referencia x 0,97)


def test_validation_rejects_nulls_and_gaps(market: SyntheticMarket) -> None:
    broken = market.power.copy()
    broken.loc[100, "spot_price_eur_mwh"] = np.nan
    with pytest.raises(DataValidationError, match="no_nulls"):
        validate_power_frame(broken, YEAR, TZ)
    gapped = market.power.drop(index=500).reset_index(drop=True)
    with pytest.raises(DataValidationError, match="row_count"):
        validate_power_frame(gapped, YEAR, TZ)


# --------------------------------------------------------------------------------------------------
# Conectores reales (respuestas simuladas)
# --------------------------------------------------------------------------------------------------


def test_esios_parser_filters_geo_and_aggregates_mtu15_to_hourly() -> None:
    session = FakeEsiosSession()
    client = EsiosClient(EsiosSettings(api_token="test-token", chunk_days=2), session=session)
    start = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
    raw = client.fetch_indicator(600, start, start + timedelta(days=5))
    assert session.calls == 3  # troceo en ventanas de 2 días
    assert (raw > 0).all()  # descartada la geografía Portugal (-999)
    hourly = raw.groupby(raw.index.floor("h")).mean()
    assert len(hourly) == 5 * 24
    assert hourly.iloc[13] == pytest.approx(56.0 + 13)


def test_esios_requires_token() -> None:
    with pytest.raises(IngestionError, match="ESIOS_API_TOKEN"):
        EsiosClient(EsiosSettings(api_token=None))


def test_mibgas_workbook_parsing_and_weekend_fill() -> None:
    content = build_mibgas_workbook(YEAR)
    parsed = MibgasClient.parse_workbook(content, "GDAES_D+1", "PVB", YEAR)
    assert parsed.loc[pd.Timestamp("2025-01-02")] == pytest.approx(round(30.0 + 2 / 50.0, 2))  # PVB, no VTP
    assert parsed.max() < 99.0  # excluido el producto mensual
    client = MibgasClient(MibgasSettings(), session=FakeMibgasSession(content))
    series, filled = client.fetch_pvb_day_ahead(YEAR)
    assert len(series) == 365 and not series.isna().any()
    assert filled > 0  # entregas de domingo/lunes sin sesión previa


def test_live_pipeline_with_real_connectors(tmp_path: Path) -> None:
    esios_settings = EsiosSettings(api_token="test-token", chunk_days=31)
    settings = make_settings(tmp_path, mock_data=False, strict=True, esios=esios_settings)
    pipeline = MarketIngestionPipeline(
        settings,
        esios_client=EsiosClient(esios_settings, session=FakeEsiosSession()),
        mibgas_client=MibgasClient(settings.mibgas, session=FakeMibgasSession(build_mibgas_workbook(YEAR))),
    )
    result = pipeline.run()
    assert result.power.source == DataSource.ESIOS and result.gas.source == DataSource.MIBGAS
    power = load_power_market(tmp_path)
    gas = load_gas_market(tmp_path)
    assert len(power) == 8760 and set(power["data_source"]) == {"ESIOS"}
    assert len(gas) == 365 and set(gas["weather_source"]) == {"CLIMATOLOGY"}
    expected_ancillary = sum(50.0 + i / 100.0 for i in (807, 808, 809, 810, 811, 813, 814, 815))
    first = power.iloc[0]  # 23:00 UTC
    assert first["ancillary_services_cost_eur_mwh"] == pytest.approx(round(expected_ancillary + 8 * 23, 2))


def test_live_mode_falls_back_to_synthetic_with_audit_trail(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mock_data=False, strict=False)
    pipeline = MarketIngestionPipeline(settings, mibgas_client=MibgasClient(settings.mibgas, session=FailingSession()))
    result = pipeline.run()
    assert result.power.source == DataSource.SYNTHETIC
    assert "ESIOS_API_TOKEN" in (result.power.fallback_reason or "")
    assert result.gas.source == DataSource.SYNTHETIC
    assert "network unreachable" in (result.gas.fallback_reason or "")
    assert result.cross_commodity_coherent


def test_strict_mode_raises_instead_of_fallback(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mock_data=False, strict=True)
    with pytest.raises(IngestionError, match="INGESTION_STRICT"):
        MarketIngestionPipeline(settings).run()


def test_esios_gap_above_threshold_triggers_fallback(tmp_path: Path) -> None:
    esios_settings = EsiosSettings(api_token="test-token", chunk_days=31)
    settings = make_settings(tmp_path, mock_data=False, esios=esios_settings, max_imputed_ratio=0.001)
    pipeline = MarketIngestionPipeline(
        settings,
        esios_client=EsiosClient(esios_settings, session=FakeEsiosSession(missing_hours=3)),
        mibgas_client=MibgasClient(settings.mibgas, session=FakeMibgasSession(build_mibgas_workbook(YEAR))),
    )
    result = pipeline.run()
    assert result.power.source == DataSource.SYNTHETIC
    assert "superan el umbral" in (result.power.fallback_reason or "")
    assert not result.cross_commodity_coherent  # gas real + electricidad sintética


# --------------------------------------------------------------------------------------------------
# Persistencia y linaje
# --------------------------------------------------------------------------------------------------


def test_parquet_persistence_schema_and_manifest(tmp_path: Path) -> None:
    result = MarketIngestionPipeline(make_settings(tmp_path)).run()
    power_table = pq.read_table(tmp_path / "power_market.parquet")
    gas_table = pq.read_table(tmp_path / "gas_market.parquet")
    assert power_table.schema.remove_metadata().equals(power_arrow_schema(TZ))
    assert gas_table.schema.remove_metadata().equals(GAS_ARROW_SCHEMA)
    assert power_table.num_rows == 8760 and gas_table.num_rows == 365
    assert sum(col.null_count for col in power_table.columns) == 0
    assert sum(col.null_count for col in gas_table.columns) == 0
    lineage = json.loads(power_table.schema.metadata[b"met.lineage"])
    assert lineage["source"] == "SYNTHETIC" and lineage["seed"] == 7
    manifest = json.loads((tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["power"]["sha256"] == result.power.sha256
    assert manifest["power"]["quality"]["checks"]["ancillary_sum_to_the_cent"] is True
    assert not list(tmp_path.glob("*.tmp"))
