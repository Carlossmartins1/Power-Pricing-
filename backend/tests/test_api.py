"""Tests de la API REST con `TestClient` sobre un Data Warehouse aislado (modo sintético)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

EFFECTS = ["volume_effect_eur", "shape_effect_eur", "wholesale_hedging_effect_eur", "imbalance_effect_eur"]


def cents(value: float) -> int:
    return int(round(value * 100))


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("warehouse")


@pytest.fixture(scope="module")
def client(warehouse: Path) -> Iterator[TestClient]:
    app = create_app(Settings(mock_data=True, warehouse_dir=warehouse, random_seed=42))
    with TestClient(app) as test_client:  # ejecuta el lifespan: ingesta + carga en memoria
        yield test_client


def test_startup_bootstraps_empty_warehouse(client: TestClient, warehouse: Path) -> None:
    assert (warehouse / "power_market.parquet").exists() and (warehouse / "gas_market.parquet").exists()
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["manifest_found"] is True
    assert body["market_year"] == 2025 and body["pipeline_version"] == "1.0.0"
    assert body["power"] == {**body["power"], "rows": 8760, "source": "SYNTHETIC", "quality_passed": True}
    assert body["gas"]["rows"] == 365 and len(body["power"]["sha256"]) == 64
    assert 0.3 < body["cross_commodity_correlation"] < 0.9


def test_power_quote_endpoint(client: TestClient) -> None:
    payload = {
        "tariff": "3.0TD",
        "profile": "COMMERCIAL_DAY",
        "product": "FIXED_TOU",
        "annual_consumption_mwh": 850,
        "forecast_error_pct": 6,
        "margin_eur_mwh": 3.5,
        "contracted_power_kw": 120,
    }
    response = client.post("/api/v1/price-quote/power", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert sum(cents(c["value_eur_mwh"]) for c in body["build_up"]) == cents(body["kpis"]["final_price_eur_mwh"])
    assert [p["period"] for p in body["period_prices"]] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    assert [h["hour"] for h in body["hourly_profile"]] == list(range(24))
    assert body["kpis"]["margin_eur_mwh"] == 3.5 and body["kpis"]["power_term_eur_year"] > 0
    assert body["kpis"]["shape_cost_eur_mwh"] == pytest.approx(
        body["kpis"]["weighted_omie_eur_mwh"] - body["kpis"]["baseload_omie_eur_mwh"], abs=0.011
    )
    assert {c["code"] for c in body["build_up"]} >= {"OMIE_BASELOAD", "SHAPE", "SSAA", "IMBALANCE", "TOLLS", "CHARGES", "MARGIN"}


def test_power_quote_cap_and_monte_carlo(client: TestClient) -> None:
    payload = {"product": "FIXED_WITH_CAP", "cap_strike_eur_mwh": 85, "imbalance_method": "MONTE_CARLO", "monte_carlo_paths": 50}
    body = client.post("/api/v1/price-quote/power", json=payload).json()
    assert body["kpis"]["cap_option_premium_eur_mwh"] > 0
    assert body["kpis"]["imbalance_cost_p95_eur_mwh"] >= body["kpis"]["imbalance_cost_eur_mwh"]


def test_invalid_requests_return_422(client: TestClient) -> None:
    negative = client.post("/api/v1/price-quote/power", json={"annual_consumption_mwh": -5})
    assert negative.status_code == 422
    unknown_field = client.post("/api/v1/price-quote/gas", json={"tarifa": "RL.9"})
    assert unknown_field.status_code == 422
    wrong_length = client.post("/api/v1/price-quote/power", json={"profile": "CUSTOM", "custom_profile": [1.0, 2.0, 3.0]})
    assert wrong_length.status_code == 422
    assert wrong_length.json() == {"detail": "La curva personalizada debe tener 8760 valores horarios (recibidos: 3)", "error_type": "pricing_error"}


def test_gas_quote_endpoint(client: TestClient) -> None:
    for tariff, volume in (("RL.1", 3), ("RL.2", 9), ("RL.3", 30), ("RL.4", 150)):
        response = client.post("/api/v1/price-quote/gas", json={"tariff": tariff, "annual_consumption_mwh": volume})
        assert response.status_code == 200
        body = response.json()
        assert sum(cents(c["value_eur_mwh"]) for c in body["build_up"]) == cents(body["kpis"]["final_price_eur_mwh"])
        assert body["kpis"]["seasonal_shape_cost_eur_mwh"] > 0 and len(body["monthly"]) == 12
        january, july = body["monthly"][0], body["monthly"][6]
        assert january["consumption_mwh"] > 3 * july["consumption_mwh"] and january["hdd"] > july["hdd"]


def test_portfolio_backtest_endpoint(client: TestClient) -> None:
    payload = {
        "n_contracts": 500,
        "stress": {"volume_shock_pct": 15, "shape_shift_pct": 10, "imbalance_multiplier": 2, "unhedged_ratio": 0.3},
        "margin_threshold_eur_mwh": 2.0,
        "max_alerts": 10,
    }
    response = client.post("/api/v1/portfolio/backtest", json=payload)
    assert response.status_code == 200
    body = response.json()
    steps = {s["key"]: s for s in body["waterfall"]}
    assert cents(steps["ex_ante"]["value_eur"]) + sum(cents(steps[k]["value_eur"]) for k in EFFECTS) == cents(steps["ex_post"]["value_eur"])
    assert body["kpis"]["contracts"] == 500 and body["kpis"]["contracts_at_risk"] >= len(body["at_risk_contracts"]) == 10
    assert all(c["alert"] == "REPRICE_REQUIRED" for c in body["at_risk_contracts"])
    assert {s["tariff"] for s in body["segments"]} == {"2.0TD", "3.0TD", "6.1TD", "RL.1", "RL.2", "RL.3"}
    out_of_range = client.post("/api/v1/portfolio/backtest", json={"stress": {"unhedged_ratio": 1.5}})
    assert out_of_range.status_code == 422


def test_market_intelligence_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/market/intelligence").json()
    assert len(body["hourly"]) == 24 and len(body["monthly"]) == 12 and len(body["correlation"]) == 365
    hourly = {h["hour"]: h for h in body["hourly"]}
    assert hourly[14]["spot_mean"] < hourly[20]["spot_mean"]  # curva de pato
    assert hourly[14]["spot_spring"] < hourly[14]["spot_winter"]
    assert hourly[19]["short_premium_mean"] > hourly[3]["short_premium_mean"]  # rampa de tarde
    assert hourly[9]["long_discount_mean"] > hourly[3]["long_discount_mean"]  # rampa de mañana
    summary = body["summary"]
    assert summary["duck_curve_spread_eur_mwh"] > 30 and summary["zero_or_negative_hours"] > 100
    assert 0.3 < summary["power_gas_correlation"] < 0.9
    assert body["monthly"][8]["ancillary_total_eur_mwh"] > body["monthly"][1]["ancillary_total_eur_mwh"]
    for month in body["monthly"]:
        parts = month["ancillary_constraints_eur_mwh"] + month["ancillary_reserves_eur_mwh"] + month["ancillary_other_eur_mwh"]
        assert parts == pytest.approx(month["ancillary_total_eur_mwh"], abs=0.02)


def test_market_intelligence_ranks_archetypes_by_shape_cost(client: TestClient) -> None:
    shapes = {s["profile"]: s for s in client.get("/api/v1/market/intelligence").json()["archetype_shapes"]}
    assert set(shapes) == {"INDUSTRIAL_FLAT", "COMMERCIAL_DAY", "RESIDENTIAL_EVENING", "SOLAR_PROSUMER", "SOLAR_ALIGNED_INDUSTRIAL"}
    assert shapes["SOLAR_PROSUMER"]["shape_cost_eur_mwh"] > shapes["RESIDENTIAL_EVENING"]["shape_cost_eur_mwh"] > 0
    assert shapes["SOLAR_ALIGNED_INDUSTRIAL"]["shape_cost_eur_mwh"] < shapes["COMMERCIAL_DAY"]["shape_cost_eur_mwh"] < 0
    assert shapes["INDUSTRIAL_FLAT"]["shape_cost_eur_mwh"] == 0.0 and shapes["INDUSTRIAL_FLAT"]["shape_factor"] == 1.0
    assert shapes["SOLAR_ALIGNED_INDUSTRIAL"]["solar_window_share_pct"] > 80 > 20 > shapes["SOLAR_PROSUMER"]["solar_window_share_pct"]
    # Mismo número que da el cotizador para ese perfil: una sola definición de Shape Cost en toda la API.
    for profile, point in shapes.items():
        quote = client.post("/api/v1/price-quote/power", json={"profile": profile}).json()
        assert quote["kpis"]["shape_cost_eur_mwh"] == point["shape_cost_eur_mwh"]


def test_regulatory_parameters_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/regulatory/parameters").json()
    by_key = {p["key"]: p for p in body["parameters"]}
    assert set(body["status_legend"]) == {p["status"] for p in body["parameters"]}
    assert by_key["power.losses"]["status"] == "OFICIAL" and "Circular CNMC 3/2020" in by_key["power.losses"]["source"]
    assert by_key["power.fnee"]["status"] == "REFERENCIA_OFICIAL" and by_key["power.fnee"]["env_var"] == "PRICING_FNEE_EUR_MWH"
    assert by_key["gas.shrinkage"]["status"] == "CONFIGURABLE"
    assert "2027" in body["gas_reference"] and "2026" in body["power_reference"]


def test_cors_allows_dashboard_origin_only(client: TestClient) -> None:
    preflight = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"}
    for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
        allowed = client.options("/api/v1/price-quote/power", headers={**preflight, "Origin": origin})
        assert allowed.status_code == 200 and allowed.headers["access-control-allow-origin"] == origin
    rejected = client.options("/api/v1/price-quote/power", headers={**preflight, "Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in rejected.headers


def test_unhandled_errors_return_json_500(warehouse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(mock_data=True, warehouse_dir=warehouse, random_seed=42))
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        def explode(*_: object, **__: object) -> None:
            raise RuntimeError("fallo inesperado")

        monkeypatch.setattr(app.state.store.gas_engine, "quote", explode)
        response = failing_client.post("/api/v1/price-quote/gas", json={})
        assert response.status_code == 500
        assert response.json() == {"detail": "Error interno del motor de pricing", "error_type": "RuntimeError"}
