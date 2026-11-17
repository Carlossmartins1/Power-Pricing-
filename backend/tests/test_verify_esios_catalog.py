"""Tests del diagnóstico de fuentes reales (`scripts/verify_esios_catalog.py`) con ESIOS y MIBGAS simulados."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.core.config import EsiosSettings, MibgasSettings, Settings
from app.pipelines.market_ingestion import EsiosClient, IngestionError, MibgasClient, load_gas_market, load_power_market
from scripts import verify_esios_catalog as diag
from tests.test_market_ingestion import FakeEsiosSession, FakeMibgasSession, FakeResponse, build_mibgas_workbook

TOKEN = "token-de-prueba-que-no-debe-aparecer-1234"
YEAR = 2025
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


class CatalogSession(FakeEsiosSession):
    """ESIOS simulado con catálogo (`/indicators`) e indicadores que pueden faltar o venir vacíos."""

    def __init__(self, missing_ids: frozenset[int] = frozenset(), empty_ids: frozenset[int] = frozenset()) -> None:
        super().__init__()
        self.missing_ids = missing_ids
        self.empty_ids = empty_ids

    def get(self, url: str, params: Any = None, timeout: float | None = None, **kwargs: Any) -> FakeResponse:  # type: ignore[override]
        if url.rstrip("/").endswith("/indicators"):
            ids = [600, 686, 687, 807, 808, 809, 810, 811, 813, 814, 815, 2197]
            names = {686: "Precio de desvíos a subir", 687: "Precio de desvíos a bajar", 2197: "Precio desvíos bajar ISP 15 min"}
            items = [{"id": i, "name": names.get(i, f"Indicador {i}"), "short_name": ""} for i in ids if i not in self.missing_ids]
            return FakeResponse(200, {"indicators": items})
        indicator_id = int(url.rstrip("/").split("/")[-1])
        if indicator_id in self.empty_ids:
            return FakeResponse(200, {"indicator": {"id": indicator_id, "values": []}})
        return super().get(url, params=params, timeout=timeout, **kwargs)


def _settings(tmp_path: Path, token: str | None = TOKEN) -> Settings:
    return Settings(
        mock_data=False,
        market_year=YEAR,
        warehouse_dir=tmp_path,
        esios=EsiosSettings(api_token=token, chunk_days=31),
        mibgas=MibgasSettings(),
    )


def test_probe_windows_cover_january_and_the_latest_published_week() -> None:
    windows = diag.probe_windows(YEAR, NOW)
    assert windows[0][0] == datetime(YEAR, 1, 1, tzinfo=timezone.utc)
    assert windows[-1][1] == datetime(YEAR + 1, 1, 1, tzinfo=timezone.utc)
    current = diag.probe_windows(2026, NOW)
    assert current[-1][1] <= NOW  # en el año en curso no se piden fechas futuras


def test_healthy_sources_pass_and_the_token_is_never_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    esios = EsiosClient(settings.esios, session=CatalogSession())
    mibgas = MibgasClient(settings.mibgas, session=FakeMibgasSession(build_mibgas_workbook(YEAR)))
    assert diag.check_esios(settings, NOW, client=esios)
    assert diag.check_mibgas(settings, client=mibgas)
    output = capsys.readouterr().out
    assert TOKEN not in output and "…1234" in output
    assert "12 indicadores" in output and output.count("✓") == 13  # catálogo + 11 indicadores + MIBGAS


def test_missing_or_empty_indicator_fails_and_suggests_replacements(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    esios = EsiosClient(settings.esios, session=CatalogSession(missing_ids=frozenset({687}), empty_ids=frozenset({687})))
    assert not diag.check_esios(settings, NOW, client=esios)
    output = capsys.readouterr().out
    assert "✗   687 imbalance_down" in output
    assert "Candidatos del catálogo para sustituir 687" in output and "2197" in output


def test_missing_token_is_reported_without_calling_esios(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not diag.check_esios(_settings(tmp_path, token=None), NOW)
    assert "Falta ESIOS_API_TOKEN" in capsys.readouterr().out


def test_catalog_rejects_an_invalid_token() -> None:
    class Unauthorized(FakeEsiosSession):
        def get(self, *_: Any, **__: Any) -> FakeResponse:  # type: ignore[override]
            return FakeResponse(401, {"message": "Unauthorized"})

    with pytest.raises(IngestionError, match="rechazó el token"):
        EsiosClient(EsiosSettings(api_token=TOKEN), session=Unauthorized()).fetch_catalog()


def test_live_strict_ingestion_writes_both_parquet_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    ok = diag.run_live_ingestion(
        settings,
        tmp_path,
        esios_client=EsiosClient(settings.esios, session=CatalogSession()),
        mibgas_client=MibgasClient(settings.mibgas, session=FakeMibgasSession(build_mibgas_workbook(YEAR))),
    )
    assert ok
    power, gas = load_power_market(tmp_path), load_gas_market(tmp_path)
    assert len(power) == len(pd.date_range(f"{YEAR}-01-01", f"{YEAR + 1}-01-01", freq="h", inclusive="left"))
    assert set(power["data_source"]) == {"ESIOS"} and set(gas["data_source"]) == {"MIBGAS"}
    assert TOKEN not in capsys.readouterr().out
