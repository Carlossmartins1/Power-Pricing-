"""Pipeline de ingesta Multi-Commodity (Electricidad OMIE/REE ESIOS + Gas Natural MIBGAS PVB).

Capa 1 del Enterprise Multi-Commodity Power & Gas Pricing & Imbalance Engine.

Responsabilidades
-----------------
1. Electricidad (REE ESIOS): precio marginal del mercado diario (ind. 600), precios de desvíos a subir
   y a bajar, y componentes de coste de servicios de ajuste. Desde la entrada del MTU15 en el mercado
   diario (1-oct-2025) ESIOS publica valores cuartohorarios; se agregan a media horaria UTC.
2. Gas natural (MIBGAS): precio diario PVB Day-Ahead (producto GDAES_D+1) a partir del fichero anual
   de negociación, más tarifas de desbalance Enagás derivadas por la regla NC BAL (ajuste menor del 3 %).
3. Modo sintético realista (MOCK_DATA=true por defecto, o fallback si falta token / falla la red):
   año completo con curva de pato, picos nocturnos, laborable vs. fin de semana, estacionalidad,
   desvíos asimétricos en rampas solares y gas PVB parcialmente correlacionado con el pool eléctrico
   (los ciclos combinados marcan precio en las horas de residual alto).
4. Calidad y gobernanza: validación estricta (sin nulos, tipos, continuidad horaria, cuadre al céntimo
   de servicios de ajuste), escritura Parquet atómica con esquema PyArrow fijo y manifiesto de linaje.

Ejecución (desde `backend/`):
    python -m app.pipelines.market_ingestion --year 2025
    python -m app.pipelines.market_ingestion --live --strict
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from pydantic import BaseModel, Field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import REPO_ROOT, EsiosSettings, MibgasSettings, Settings, load_settings

logger = logging.getLogger("met.ingestion")

# --------------------------------------------------------------------------------------------------
# Constantes de mercado y esquemas físicos del Data Warehouse
# --------------------------------------------------------------------------------------------------

PIPELINE_VERSION: Final[str] = "1.0.0"
POWER_FILENAME: Final[str] = "power_market.parquet"
GAS_FILENAME: Final[str] = "gas_market.parquet"
MANIFEST_FILENAME: Final[str] = "_ingestion_manifest.json"

# Límites armonizados SDAC (precio mínimo / máximo de casación del mercado diario).
OMIE_PRICE_FLOOR: Final[float] = -500.0
OMIE_PRICE_CAP: Final[float] = 4000.0
# Rango de sanidad para precios de desvío (pueden exceder el cap del diario en eventos extremos).
IMBALANCE_SANITY_RANGE: Final[tuple[float, float]] = (-5000.0, 15000.0)
GAS_SANITY_RANGE: Final[tuple[float, float]] = (0.0, 500.0)
HDD_BASE_C: Final[float] = 18.0

POWER_COLUMNS: Final[tuple[str, ...]] = (
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
    "ancillary_constraints_eur_mwh",
    "ancillary_reserves_eur_mwh",
    "ancillary_other_eur_mwh",
    "ancillary_services_cost_eur_mwh",
    "data_source",
)

GAS_COLUMNS: Final[tuple[str, ...]] = (
    "gas_day",
    "month",
    "gas_season",
    "is_weekend",
    "pvb_da_price_eur_mwh",
    "imbalance_buy_price_eur_mwh",
    "imbalance_sell_price_eur_mwh",
    "temperature_c",
    "hdd_18c",
    "weather_source",
    "data_source",
)

PRICE_DECIMALS: Final[int] = 2


def power_arrow_schema(tz: str) -> pa.Schema:
    return pa.schema(
        [
            pa.field("datetime_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("datetime_local", pa.timestamp("us", tz=tz), nullable=False),
            pa.field("date_local", pa.date32(), nullable=False),
            pa.field("hour_local", pa.int8(), nullable=False),
            pa.field("month", pa.int8(), nullable=False),
            pa.field("is_weekend", pa.bool_(), nullable=False),
            pa.field("is_tariff_holiday", pa.bool_(), nullable=False),
            pa.field("spot_price_eur_mwh", pa.float64(), nullable=False),
            pa.field("imbalance_price_up_eur_mwh", pa.float64(), nullable=False),
            pa.field("imbalance_price_down_eur_mwh", pa.float64(), nullable=False),
            pa.field("ancillary_constraints_eur_mwh", pa.float64(), nullable=False),
            pa.field("ancillary_reserves_eur_mwh", pa.float64(), nullable=False),
            pa.field("ancillary_other_eur_mwh", pa.float64(), nullable=False),
            pa.field("ancillary_services_cost_eur_mwh", pa.float64(), nullable=False),
            pa.field("data_source", pa.string(), nullable=False),
        ]
    )


GAS_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("gas_day", pa.date32(), nullable=False),
        pa.field("month", pa.int8(), nullable=False),
        pa.field("gas_season", pa.string(), nullable=False),
        pa.field("is_weekend", pa.bool_(), nullable=False),
        pa.field("pvb_da_price_eur_mwh", pa.float64(), nullable=False),
        pa.field("imbalance_buy_price_eur_mwh", pa.float64(), nullable=False),
        pa.field("imbalance_sell_price_eur_mwh", pa.float64(), nullable=False),
        pa.field("temperature_c", pa.float64(), nullable=False),
        pa.field("hdd_18c", pa.float64(), nullable=False),
        pa.field("weather_source", pa.string(), nullable=False),
        pa.field("data_source", pa.string(), nullable=False),
    ]
)


class DataSource(str, Enum):
    ESIOS = "ESIOS"
    MIBGAS = "MIBGAS"
    SYNTHETIC = "SYNTHETIC"


class IngestionError(RuntimeError):
    """Fallo de obtención o de integridad de una fuente externa."""


class DataValidationError(ValueError):
    """El dataset no supera los controles de calidad obligatorios."""


# --------------------------------------------------------------------------------------------------
# Calendario regulatorio
# --------------------------------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anónimo / Meeus-Jones-Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def spanish_tariff_holidays(year: int) -> frozenset[date]:
    """Festivos nacionales de fecha fija no sustituibles (tratados como P6 en peajes 3.0TD/6.1TD).

    Circular CNMC 3/2020: se excluyen los festivos sustituibles y los que no tienen fecha fija
    (p. ej. Viernes Santo), por lo que no forman parte de esta lista.
    """
    fixed = ((1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25))
    return frozenset(date(year, m, d) for m, d in fixed)


def low_demand_days(year: int) -> frozenset[date]:
    """Días de baja demanda para el modelo sintético: festivos tarifarios + Jueves/Viernes Santo."""
    easter = easter_sunday(year)
    return spanish_tariff_holidays(year) | {easter - timedelta(days=3), easter - timedelta(days=2)}


def hourly_utc_grid(year: int, tz: str) -> pd.DatetimeIndex:
    """Rejilla horaria UTC que cubre el año civil local completo (8.760 h, 8.784 en bisiesto).

    Se trabaja en UTC para evitar ambigüedades del cambio de hora (día de 23 h y día de 25 h).
    """
    start = pd.Timestamp(year=year, month=1, day=1, tz=tz).tz_convert("UTC")
    end = pd.Timestamp(year=year + 1, month=1, day=1, tz=tz).tz_convert("UTC")
    return pd.date_range(start=start, end=end, freq="h", inclusive="left", name="datetime_utc")


def daily_grid(year: int) -> pd.DatetimeIndex:
    return pd.date_range(start=f"{year}-01-01", end=f"{year}-12-31", freq="D", name="gas_day")


def climatological_temperature_c(days: pd.DatetimeIndex) -> np.ndarray:
    """Temperatura normal diaria (media ponderada por demanda peninsular, ~5,5 °C ene / ~24,5 °C jul)."""
    doy = days.day_of_year.to_numpy(dtype=float)
    return 15.0 + 9.5 * np.sin(2.0 * np.pi * (doy - 110.0) / 365.25)


def gas_season(days: pd.DatetimeIndex) -> np.ndarray:
    """Temporada del año gasista: invierno oct-mar, verano abr-sep."""
    return np.where(days.month.isin([10, 11, 12, 1, 2, 3]), "WINTER", "SUMMER")


# --------------------------------------------------------------------------------------------------
# Utilidades HTTP
# --------------------------------------------------------------------------------------------------


def build_http_session(max_retries: int, user_agent: str) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": user_agent})
    return session


def _complete_hourly_series(
    raw: pd.Series, grid: pd.DatetimeIndex, max_imputed_ratio: float, label: str
) -> tuple[pd.Series, int]:
    """Agrega a media horaria UTC, reindexa a la rejilla e imputa huecos residuales (interpolación temporal)."""
    if raw.empty:
        raise IngestionError(f"{label}: la fuente no devolvió valores")
    hourly = raw.groupby(raw.index.floor("h")).mean()
    hourly = hourly[~hourly.index.duplicated(keep="first")].reindex(grid)
    missing = int(hourly.isna().sum())
    ratio = missing / len(grid)
    if ratio > max_imputed_ratio:
        raise IngestionError(
            f"{label}: {missing} horas sin dato ({ratio:.2%}) superan el umbral permitido ({max_imputed_ratio:.2%})"
        )
    if missing:
        hourly = hourly.interpolate(method="time", limit_direction="both")
    return hourly.astype("float64"), missing


# --------------------------------------------------------------------------------------------------
# Conector REE ESIOS
# --------------------------------------------------------------------------------------------------


class EsiosClient:
    """Cliente de la API REST de ESIOS con troceo temporal, reintentos y normalización a UTC horario."""

    ACCEPT_HEADER: Final[str] = "application/json; application/vnd.esios-api-v1+json"

    def __init__(self, settings: EsiosSettings, session: requests.Session | None = None) -> None:
        if not settings.api_token:
            raise IngestionError("ESIOS_API_TOKEN no configurado")
        self._settings = settings
        self._session = session or build_http_session(settings.max_retries, f"met-pricing-engine/{PIPELINE_VERSION}")
        self._session.headers.update(
            {
                "Accept": self.ACCEPT_HEADER,
                "Content-Type": "application/json",
                "x-api-key": settings.api_token,
            }
        )

    @staticmethod
    def parse_indicator_payload(payload: Mapping[str, Any], geo_id: int) -> pd.Series:
        """Convierte la respuesta JSON de `/indicators/{id}` en una serie indexada en UTC."""
        indicator = payload.get("indicator")
        if not isinstance(indicator, Mapping):
            raise IngestionError("Respuesta ESIOS sin bloque 'indicator'")
        values = indicator.get("values")
        if not isinstance(values, list):
            raise IngestionError("Respuesta ESIOS sin lista 'values'")
        if not values:
            return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
        frame = pd.DataFrame(values)
        if "value" not in frame.columns:
            raise IngestionError("Respuesta ESIOS sin campo 'value'")
        if "geo_id" in frame.columns and frame["geo_id"].nunique() > 1:
            frame = frame[frame["geo_id"] == geo_id]
        ts_col = "datetime_utc" if "datetime_utc" in frame.columns else "datetime"
        if ts_col not in frame.columns:
            raise IngestionError("Respuesta ESIOS sin marca temporal")
        index = pd.DatetimeIndex(pd.to_datetime(frame[ts_col], utc=True))
        series = pd.Series(pd.to_numeric(frame["value"], errors="coerce").to_numpy(dtype="float64"), index=index)
        return series.dropna().sort_index()

    def _request_chunk(self, indicator_id: int, start_utc: datetime, end_utc: datetime) -> pd.Series:
        params: list[tuple[str, str]] = [
            ("start_date", start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("end_date", end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("time_trunc", "hour"),
            ("time_agg", "average"),
            ("geo_ids[]", str(self._settings.geo_id)),
        ]
        url = f"{self._settings.base_url.rstrip('/')}/indicators/{indicator_id}"
        response = self._session.get(url, params=params, timeout=self._settings.timeout_s)
        if response.status_code in (401, 403):
            raise IngestionError(f"ESIOS rechazó el token (HTTP {response.status_code})")
        if response.status_code != 200:
            raise IngestionError(f"ESIOS indicador {indicator_id}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IngestionError(f"ESIOS indicador {indicator_id}: JSON inválido") from exc
        return self.parse_indicator_payload(payload, self._settings.geo_id)

    def fetch_indicator(self, indicator_id: int, start_utc: datetime, end_utc: datetime) -> pd.Series:
        """Descarga un indicador troceando el rango [start, end) en ventanas de `chunk_days`."""
        chunks: list[pd.Series] = []
        cursor = start_utc
        step = timedelta(days=self._settings.chunk_days)
        while cursor < end_utc:
            chunk_end = min(cursor + step, end_utc)
            chunk = self._request_chunk(indicator_id, cursor, chunk_end - timedelta(seconds=1))
            if not chunk.empty:
                chunks.append(chunk)
            cursor = chunk_end
        if not chunks:
            raise IngestionError(f"ESIOS indicador {indicator_id}: sin datos en el rango solicitado")
        series = pd.concat(chunks).sort_index()
        series = series[~series.index.duplicated(keep="last")]
        return series[(series.index >= start_utc) & (series.index < end_utc)]

    def fetch_power_market(
        self, year: int, tz: str, max_imputed_ratio: float
    ) -> tuple[dict[str, pd.Series], dict[str, int]]:
        """Descarga el set completo de electricidad y devuelve series horarias + horas imputadas por serie."""
        grid = hourly_utc_grid(year, tz)
        start_utc = grid[0].to_pydatetime()
        end_utc = (grid[-1] + pd.Timedelta(hours=1)).to_pydatetime()
        s = self._settings

        def hourly(indicator_id: int, label: str) -> tuple[pd.Series, int]:
            logger.info("ESIOS: descargando indicador %s (%s)", indicator_id, label)
            raw = self.fetch_indicator(indicator_id, start_utc, end_utc)
            return _complete_hourly_series(raw, grid, max_imputed_ratio, f"ESIOS {indicator_id} ({label})")

        def component_sum(ids: Sequence[int], label: str) -> tuple[pd.Series, int]:
            if not ids:
                return pd.Series(0.0, index=grid, dtype="float64"), 0
            parts = [hourly(i, f"{label}:{i}") for i in ids]
            total = sum((p[0] for p in parts), start=pd.Series(0.0, index=grid, dtype="float64"))
            return total, sum(p[1] for p in parts)

        spot, m_spot = hourly(s.indicator_spot, "spot OMIE")
        up, m_up = hourly(s.indicator_imbalance_up, "desvíos a subir")
        down, m_down = hourly(s.indicator_imbalance_down, "desvíos a bajar")
        constraints, m_c = component_sum(s.ancillary_constraints_ids, "restricciones")
        reserves, m_r = component_sum(s.ancillary_reserves_ids, "reservas")
        other, m_o = component_sum(s.ancillary_other_ids, "otros ajuste")

        series = {
            "spot": spot,
            "up": up,
            "down": down,
            "constraints": constraints,
            "reserves": reserves,
            "other": other,
        }
        imputed = {
            "spot": m_spot,
            "imbalance_up": m_up,
            "imbalance_down": m_down,
            "ancillary_constraints": m_c,
            "ancillary_reserves": m_r,
            "ancillary_other": m_o,
        }
        return series, imputed


# --------------------------------------------------------------------------------------------------
# Conector MIBGAS
# --------------------------------------------------------------------------------------------------


def _normalize_header(value: Any) -> str:
    text = str(value).strip().lower()
    text = (
        text.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    )
    return re.sub(r"\s+", " ", text)


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float, np.floating, np.integer)):
        return float(value)
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return math.nan


class MibgasClient:
    """Descarga y parsea el fichero anual de MIBGAS para extraer el precio Day-Ahead del PVB."""

    PRICE_COLUMN_PRIORITY: Final[tuple[str, ...]] = (
        "reference price",
        "precio de referencia",
        "daily reference price",
        "last price",
        "ultimo precio",
        "precio ultimo",
        "average price",
        "precio medio",
    )

    def __init__(self, settings: MibgasSettings, session: requests.Session | None = None) -> None:
        self._settings = settings
        self._session = session or build_http_session(settings.max_retries, f"met-pricing-engine/{PIPELINE_VERSION}")

    def download_year(self, year: int) -> bytes:
        url = self._settings.url_template.format(year=year)
        logger.info("MIBGAS: descargando %s", url)
        response = self._session.get(url, timeout=self._settings.timeout_s)
        if response.status_code != 200:
            raise IngestionError(f"MIBGAS {year}: HTTP {response.status_code}")
        content = response.content
        if not content.startswith(b"PK"):
            raise IngestionError(f"MIBGAS {year}: la respuesta no es un fichero XLSX válido")
        return content

    @classmethod
    def _locate_table(cls, raw: pd.DataFrame) -> pd.DataFrame | None:
        """Detecta la fila de cabecera (la que contiene la columna de producto) dentro de una hoja."""
        scan_rows = min(len(raw), 25)
        for row_idx in range(scan_rows):
            headers = [_normalize_header(v) for v in raw.iloc[row_idx].tolist()]
            if any(h.startswith("product") or h.startswith("producto") for h in headers):
                table = raw.iloc[row_idx + 1 :].copy()
                table.columns = headers
                return table.dropna(how="all")
        return None

    @classmethod
    def parse_workbook(cls, content: bytes, product_code: str, area: str, year: int) -> pd.Series:
        """Extrae la serie diaria (por día de entrega) del producto Day-Ahead del PVB."""
        sheets: dict[str, pd.DataFrame] = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine="openpyxl")
        target = product_code.strip().upper()
        collected: list[pd.Series] = []
        for sheet_name, raw in sheets.items():
            table = cls._locate_table(raw)
            if table is None:
                continue
            columns = list(table.columns)
            product_col = next(c for c in columns if c.startswith("product") or c.startswith("producto"))
            mask = table[product_col].astype(str).str.strip().str.upper() == target
            if not mask.any():
                continue
            subset = table[mask]
            area_col = next((c for c in columns if c in {"area", "zona", "market area"} or c.startswith("area")), None)
            if area_col is not None:
                area_values = subset[area_col].astype(str).str.strip().str.upper()
                if (area_values == area.upper()).any():
                    subset = subset[area_values == area.upper()]
            price_col = next(
                (c for key in cls.PRICE_COLUMN_PRIORITY for c in columns if key in c and "volume" not in c),
                None,
            )
            if price_col is None:
                continue
            delivery_col = next(
                (c for c in columns if "delivery" in c or "entrega" in c or "suministro" in c), None
            )
            if delivery_col is not None:
                days = pd.to_datetime(subset[delivery_col], dayfirst=True, errors="coerce")
            else:
                trading_col = next(
                    (c for c in columns if "trading day" in c or "fecha" in c or "sesion" in c or c == "date"), None
                )
                if trading_col is None:
                    continue
                days = pd.to_datetime(subset[trading_col], dayfirst=True, errors="coerce") + pd.Timedelta(days=1)
            prices = subset[price_col].map(_to_float)
            series = pd.Series(prices.to_numpy(dtype="float64"), index=pd.DatetimeIndex(days).normalize())
            series = series[series.index.notna() & series.notna()]
            if not series.empty:
                logger.info("MIBGAS: hoja '%s' -> %d cotizaciones %s", sheet_name, len(series), target)
                collected.append(series)
        if not collected:
            raise IngestionError(f"MIBGAS: no se encontró el producto {product_code} en el fichero")
        combined = pd.concat(collected).sort_index()
        combined = combined.groupby(level=0).mean()
        return combined[combined.index.year == year]

    def fetch_pvb_day_ahead(self, year: int) -> tuple[pd.Series, int]:
        """Serie diaria completa del año; los huecos (fines de semana/festivos) se rellenan hacia delante."""
        content = self.download_year(year)
        raw = self.parse_workbook(content, self._settings.product_code, self._settings.area, year)
        grid = daily_grid(year)
        series = raw.reindex(grid)
        missing = int(series.isna().sum())
        filled = series.ffill(limit=self._settings.max_gap_days).bfill(limit=self._settings.max_gap_days)
        if filled.isna().any():
            raise IngestionError(
                f"MIBGAS {year}: huecos superiores a {self._settings.max_gap_days} días consecutivos sin cotización"
            )
        return filled.astype("float64"), missing


def gas_imbalance_prices_nc_bal(
    reference_price: np.ndarray,
    small_adjustment: float,
    tso_buy_price: np.ndarray | None = None,
    tso_sell_price: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Tarifas de desbalance diario en PVB (Reg. UE 312/2014, art. 22; Resolución CNMC de 1-jul-2020).

    - Compra (usuario corto): máx(precio más alto de compra del GTS, precio de referencia × (1 + ajuste)).
    - Venta (usuario largo): mín(precio más bajo de venta del GTS, precio de referencia × (1 − ajuste)).

    El ajuste menor vigente es el 3 % (la Resolución de 1-jul-2020 lo elevó desde el 2,5 %). Desde la
    modificación de 28-jul-2022 el precio de referencia oficial es el del producto intradiario (WD); aquí se
    aproxima con el Day-Ahead, que es la serie disponible en el warehouse.
    """
    buy = reference_price * (1.0 + small_adjustment)
    sell = reference_price * (1.0 - small_adjustment)
    if tso_buy_price is not None:
        buy = np.fmax(buy, tso_buy_price)
    if tso_sell_price is not None:
        sell = np.fmin(sell, tso_sell_price)
    return buy, sell


# --------------------------------------------------------------------------------------------------
# Generador sintético realista (mercado ibérico)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticCalibration:
    """Parámetros calibrados a órdenes de magnitud del mercado español 2024-2025."""

    # Gas natural MIBGAS PVB (log-OU con estacionalidad y sensibilidad a temperatura)
    gas_mean_eur_mwh: float = 36.0
    gas_seasonal_amplitude: float = 0.13
    gas_hdd_sensitivity: float = 0.012
    gas_mean_reversion: float = 0.06
    gas_daily_vol: float = 0.028
    gas_wind_sensitivity: float = 0.5
    gas_tso_action_probability: float = 0.04
    # Derechos de emisión EUA (OU aritmético)
    eua_mean_eur_t: float = 72.0
    eua_mean_reversion: float = 0.04
    eua_daily_vol: float = 1.1
    # Ciclo combinado marginal
    ccgt_efficiency: float = 0.53
    ccgt_emission_factor_t_mwh: float = 0.365
    ccgt_vom_eur_mwh: float = 3.0
    # Demanda peninsular y parque renovable
    demand_mean_mw: float = 29_000.0
    pv_capacity_mw: float = 32_000.0
    wind_capacity_mw: float = 31_500.0
    must_run_mw: float = 8_000.0
    flexible_absorption_mw: float = 4_000.0
    # Curva de oferta residual (logística sobre la demanda residual)
    supply_midpoint_mw: float = 1_500.0
    supply_width_mw: float = 2_800.0
    scarcity_threshold_mw: float = 17_000.0
    scarcity_eur_per_gw2: float = 0.9
    surplus_price_mean: float = 2.0
    surplus_price_vol: float = 3.0
    hourly_noise_eur: float = 5.0
    hourly_noise_ar: float = 0.7
    # Desvíos REE (precio dual)
    imbalance_base_spread_eur: float = 1.5
    imbalance_short_pct: float = 0.07
    imbalance_long_pct: float = 0.08
    imbalance_ramp_weight: float = 1.4
    imbalance_vol_weight: float = 0.6
    # Servicios de ajuste
    constraints_base_eur: float = 2.5
    constraints_solar_weight: float = 7.0
    constraints_reinforced_uplift: float = 5.5
    reserves_base_eur: float = 1.2
    reserves_solar_weight: float = 1.5
    reserves_reinforced_uplift: float = 1.0


@dataclass(frozen=True, slots=True)
class SyntheticMarket:
    power: pd.DataFrame
    gas: pd.DataFrame


class SyntheticMarketGenerator:
    """Genera un año de mercado coherente entre commodities (mismo escenario meteorológico y de gas)."""

    def __init__(
        self,
        year: int,
        seed: int,
        tz: str,
        reinforced_operation_start: date,
        small_adjustment: float,
        calibration: SyntheticCalibration | None = None,
    ) -> None:
        self.year = year
        self.tz = tz
        self.rng = np.random.default_rng(seed)
        self.cal = calibration or SyntheticCalibration()
        self.reinforced_start_utc = pd.Timestamp(reinforced_operation_start, tz=tz).tz_convert("UTC")
        self.small_adjustment = small_adjustment

    # ---------------- procesos diarios ----------------

    def _ar1(self, n: int, phi: float, sigma: float) -> np.ndarray:
        shocks = self.rng.normal(0.0, sigma, size=n)
        out = np.empty(n)
        out[0] = shocks[0] / math.sqrt(max(1e-9, 1 - phi**2))
        for t in range(1, n):
            out[t] = phi * out[t - 1] + shocks[t]
        return out

    def _daily_drivers(self, days: pd.DatetimeIndex) -> pd.DataFrame:
        c = self.cal
        n = len(days)
        doy = days.day_of_year.to_numpy(dtype=float)

        temp_normal = climatological_temperature_c(days)
        temp = temp_normal + self._ar1(n, phi=0.8, sigma=1.8)
        hdd = np.maximum(HDD_BASE_C - temp, 0.0)
        hdd_anomaly = hdd - np.maximum(HDD_BASE_C - temp_normal, 0.0)
        cdd = np.maximum(temp - 22.0, 0.0)

        # Eólica: AR(1) diario sobre factor de carga estacional (más viento en invierno)
        wind_seasonal = 0.24 + 0.05 * np.cos(2.0 * np.pi * (doy - 15.0) / 365.25)
        wind_cf = np.clip(wind_seasonal + self._ar1(n, phi=0.65, sigma=0.07), 0.04, 0.70)
        wind_anomaly = wind_cf - wind_seasonal

        # Gas PVB: log-OU sobre media estacional (invierno caro) + shock de frío
        log_target = (
            np.log(c.gas_mean_eur_mwh)
            + c.gas_seasonal_amplitude * np.cos(2.0 * np.pi * (doy - 15.0) / 365.25)
            + c.gas_hdd_sensitivity * hdd_anomaly
        )
        log_gas = np.empty(n)
        log_gas[0] = log_target[0]
        shocks = self.rng.normal(0.0, c.gas_daily_vol, size=n)
        for t in range(1, n):
            log_gas[t] = log_gas[t - 1] + c.gas_mean_reversion * (log_target[t] - log_gas[t - 1]) + shocks[t]
        # Day-Ahead reacciona a la previsión eólica del día siguiente: menos viento -> más consumo de CCGT
        gas = np.exp(log_gas - c.gas_wind_sensitivity * wind_anomaly)

        # Acciones de balance del GTS (Enagás): compras en días fríos/tensos, ventas en días templados
        cold = hdd_anomaly > 3.0
        tso_buy_flag = cold | (self.rng.random(n) < c.gas_tso_action_probability)
        tso_sell_flag = (~tso_buy_flag) & (self.rng.random(n) < c.gas_tso_action_probability * 0.75)
        tso_buy = np.where(
            tso_buy_flag, gas * (1.0 + self.small_adjustment + self.rng.uniform(0.005, 0.035, n)), np.nan
        )
        tso_sell = np.where(
            tso_sell_flag, gas * (1.0 - self.small_adjustment - self.rng.uniform(0.005, 0.030, n)), np.nan
        )

        # EUA (€/t)
        eua = np.empty(n)
        eua[0] = c.eua_mean_eur_t
        eua_shocks = self.rng.normal(0.0, c.eua_daily_vol, size=n)
        for t in range(1, n):
            eua[t] = eua[t - 1] + c.eua_mean_reversion * (c.eua_mean_eur_t - eua[t - 1]) + eua_shocks[t]
        eua = np.clip(eua, 40.0, 110.0)

        srmc = gas / c.ccgt_efficiency + eua * c.ccgt_emission_factor_t_mwh + c.ccgt_vom_eur_mwh
        # Valor del agua: primavera húmeda (hidráulica oferta por debajo del CCGT), otoño seco
        water_value = (
            0.97
            - 0.16 * np.exp(-(((doy - 100.0) / 38.0) ** 2))
            + 0.06 * np.exp(-(((doy - 300.0) / 45.0) ** 2))
        )
        # Nubosidad: mayor en invierno
        cloud = np.clip(
            0.80 + 0.12 * np.sin(2.0 * np.pi * (doy - 80.0) / 365.25) + self.rng.normal(0.0, 0.10, n), 0.25, 1.0
        )
        price_level_shock = np.exp(self._ar1(n, phi=0.5, sigma=0.05))

        return pd.DataFrame(
            {
                "temp": temp,
                "hdd": hdd,
                "cdd": cdd,
                "gas": gas,
                "tso_buy": tso_buy,
                "tso_sell": tso_sell,
                "srmc": srmc,
                "water_value": water_value,
                "wind_cf": wind_cf,
                "cloud": cloud,
                "price_shock": price_level_shock,
            },
            index=days,
        )

    # ---------------- proceso horario ----------------

    def _hourly_fundamentals(self, grid_utc: pd.DatetimeIndex, drivers: pd.DataFrame) -> pd.DataFrame:
        c = self.cal
        local = grid_utc.tz_convert(self.tz)
        local_days = pd.DatetimeIndex(local.tz_localize(None).normalize())
        d = drivers.reindex(local_days)
        doy = local.day_of_year.to_numpy(dtype=float)
        hour_local = local.hour.to_numpy(dtype=float) + 0.5
        hour_utc = grid_utc.hour.to_numpy(dtype=float) + 0.5
        dow = local.day_of_week.to_numpy()
        holidays = low_demand_days(self.year)
        is_holiday = np.array([day in holidays for day in local_days.date])

        # Fotovoltaica: campana centrada en el mediodía solar peninsular (~12:15 UTC)
        day_length = 12.0 + 2.8 * np.sin(2.0 * np.pi * (doy - 80.0) / 365.25)
        sunrise = 12.25 - day_length / 2.0
        phase = (hour_utc - sunrise) / day_length
        clear_sky = np.where((phase > 0) & (phase < 1), np.sin(np.pi * np.clip(phase, 0, 1)) ** 1.25, 0.0)
        peak_factor = 0.70 + 0.14 * np.sin(2.0 * np.pi * (doy - 80.0) / 365.25)
        pv = c.pv_capacity_mw * clear_sky * peak_factor * d["cloud"].to_numpy()

        # Eólica: perfil diurno suave (valle a mediodía) + ruido horario
        wind = c.wind_capacity_mw * d["wind_cf"].to_numpy() * (1.0 + 0.10 * np.cos(2.0 * np.pi * (hour_local - 3.0) / 24.0))
        wind = np.clip(wind * (1.0 + self._ar1(len(grid_utc), phi=0.9, sigma=0.03)), 0.0, c.wind_capacity_mw)

        # Demanda: valle nocturno, meseta matinal, pico de tarde-noche; verano con climatización
        summer_w = np.clip(np.sin(2.0 * np.pi * (doy - 110.0) / 365.25), 0.0, 1.0)

        def bump(center: float, width: float) -> np.ndarray:
            return np.exp(-0.5 * ((hour_local - center) / width) ** 2)

        shape = (
            0.80
            + 0.17 * bump(11.5, 3.0)
            + 0.20 * bump(20.5, 2.0) * (1.0 - 0.4 * summer_w)
            + 0.12 * bump(15.5, 3.0) * summer_w
            - 0.07 * bump(4.5, 1.8)
        )
        day_factor = np.select([is_holiday | (dow == 6), dow == 5], [0.85, 0.91], default=1.0)
        weather_factor = 1.0 + 0.013 * d["hdd"].to_numpy() + 0.022 * d["cdd"].to_numpy()
        demand = c.demand_mean_mw * shape * day_factor * weather_factor
        demand *= 1.0 + self._ar1(len(grid_utc), phi=0.85, sigma=0.008)

        return pd.DataFrame(
            {"demand": demand, "pv": pv, "wind": wind, "is_low_demand_day": is_holiday | (dow >= 5)},
            index=grid_utc,
        ).assign(**{col: d[col].to_numpy() for col in ("srmc", "water_value", "price_shock")})

    def _spot_price(self, fundamentals: pd.DataFrame) -> np.ndarray:
        c = self.cal
        f = fundamentals
        residual = f["demand"].to_numpy() - f["pv"].to_numpy() - f["wind"].to_numpy() - c.must_run_mw
        # Bombeo, baterías y exportaciones absorben parte del excedente solar
        residual = residual + np.clip(-residual, 0.0, c.flexible_absorption_mw) * 0.6
        marginal_ref = f["srmc"].to_numpy() * f["water_value"].to_numpy() * f["price_shock"].to_numpy()
        n = len(residual)
        surplus_price = c.surplus_price_mean + c.surplus_price_vol * self.rng.standard_normal(n)
        logistic = 1.0 / (1.0 + np.exp(-(residual - c.supply_midpoint_mw) / c.supply_width_mw))
        scarcity = c.scarcity_eur_per_gw2 * (np.maximum(residual - c.scarcity_threshold_mw, 0.0) / 1000.0) ** 2
        price = surplus_price + (marginal_ref - surplus_price) * logistic + scarcity
        price += self._ar1(n, phi=c.hourly_noise_ar, sigma=c.hourly_noise_eur * math.sqrt(1 - c.hourly_noise_ar**2)) * (
            0.25 + logistic
        )
        # Excedente renovable severo: precios nulos o ligeramente negativos
        deep_surplus = residual < -2_500.0
        price = np.where(deep_surplus, np.minimum(price, self.rng.uniform(-1.0, 0.5, n)), price)
        return np.round(np.clip(price, -5.0, 450.0), PRICE_DECIMALS)

    def _imbalance_prices(self, spot: np.ndarray, pv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        c = self.cal
        n = len(spot)
        spot_s = pd.Series(spot)
        vol24 = spot_s.rolling(24, min_periods=2).std().fillna(0.0).to_numpy()
        vol_norm = vol24 / max(float(vol24.mean()), 1e-6)
        dpv_gw = np.diff(pv, prepend=pv[0]) / 1000.0
        ramp_norm = np.abs(dpv_gw) / max(float(np.percentile(np.abs(dpv_gw), 99)), 1e-6)
        # Rampa descendente de la tarde (pérdida solar) -> sistema corto con más frecuencia
        p_short = np.clip(0.50 - 0.10 * dpv_gw, 0.15, 0.85)
        system_short = self.rng.random(n) < p_short
        intensity = 1.0 + c.imbalance_ramp_weight * ramp_norm + c.imbalance_vol_weight * vol_norm
        pct_short = self.rng.lognormal(math.log(c.imbalance_short_pct), 0.5, n)
        pct_long = self.rng.lognormal(math.log(c.imbalance_long_pct), 0.5, n)
        premium_short = (np.abs(spot) * pct_short + c.imbalance_base_spread_eur) * intensity * np.where(system_short, 1.8, 0.6)
        discount_long = (np.abs(spot) * pct_long + c.imbalance_base_spread_eur) * intensity * np.where(system_short, 0.6, 1.8)
        price_down = np.round(spot + premium_short, PRICE_DECIMALS)
        price_up = np.round(spot - discount_long, PRICE_DECIMALS)
        return price_up, price_down

    def _ancillary_costs(
        self, grid_utc: pd.DatetimeIndex, fundamentals: pd.DataFrame, spot: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        c = self.cal
        n = len(grid_utc)
        pv_share = np.clip(fundamentals["pv"].to_numpy() / fundamentals["demand"].to_numpy(), 0.0, 1.0)
        reinforced = (grid_utc >= self.reinforced_start_utc).astype(float)
        vol = pd.Series(spot).rolling(24, min_periods=2).std().fillna(0.0).to_numpy()
        vol_norm = vol / max(float(vol.mean()), 1e-6)
        constraints = (
            c.constraints_base_eur
            + c.constraints_solar_weight * pv_share
            + c.constraints_reinforced_uplift * reinforced
            + self.rng.normal(0.0, 1.2, n)
        )
        reserves = (
            c.reserves_base_eur
            + c.reserves_solar_weight * pv_share
            + 0.6 * vol_norm
            + c.reserves_reinforced_uplift * reinforced
            + self.rng.normal(0.0, 0.4, n)
        )
        other = self.rng.normal(0.35, 0.35, n)
        return (
            np.round(np.maximum(constraints, 0.2), PRICE_DECIMALS),
            np.round(np.maximum(reserves, 0.1), PRICE_DECIMALS),
            np.round(other, PRICE_DECIMALS),
        )

    def generate(self) -> SyntheticMarket:
        days = daily_grid(self.year)
        grid = hourly_utc_grid(self.year, self.tz)
        drivers = self._daily_drivers(days)
        fundamentals = self._hourly_fundamentals(grid, drivers)
        spot = self._spot_price(fundamentals)
        up, down = self._imbalance_prices(spot, fundamentals["pv"].to_numpy())
        constraints, reserves, other = self._ancillary_costs(grid, fundamentals, spot)

        power = build_power_frame(
            grid,
            spot=spot,
            imbalance_up=up,
            imbalance_down=down,
            constraints=constraints,
            reserves=reserves,
            other=other,
            source=DataSource.SYNTHETIC,
            tz=self.tz,
        )
        gas_price = np.round(drivers["gas"].to_numpy(), PRICE_DECIMALS)
        buy, sell = gas_imbalance_prices_nc_bal(
            gas_price, self.small_adjustment, drivers["tso_buy"].to_numpy(), drivers["tso_sell"].to_numpy()
        )
        gas = build_gas_frame(
            days,
            price=gas_price,
            imbalance_buy=buy,
            imbalance_sell=sell,
            temperature=drivers["temp"].to_numpy(),
            weather_source="SYNTHETIC",
            source=DataSource.SYNTHETIC,
        )
        return SyntheticMarket(power=power, gas=gas)


# --------------------------------------------------------------------------------------------------
# Construcción de datasets canónicos
# --------------------------------------------------------------------------------------------------


def build_power_frame(
    grid_utc: pd.DatetimeIndex,
    *,
    spot: np.ndarray | pd.Series,
    imbalance_up: np.ndarray | pd.Series,
    imbalance_down: np.ndarray | pd.Series,
    constraints: np.ndarray | pd.Series,
    reserves: np.ndarray | pd.Series,
    other: np.ndarray | pd.Series,
    source: DataSource,
    tz: str,
) -> pd.DataFrame:
    """Dataset horario canónico de electricidad (precios redondeados a céntimo de €/MWh)."""
    local = grid_utc.tz_convert(tz)
    local_dates = local.tz_localize(None).normalize()
    holidays = spanish_tariff_holidays(int(local_dates[len(local_dates) // 2].year))

    def money(values: np.ndarray | pd.Series) -> np.ndarray:
        return np.round(np.asarray(values, dtype="float64"), PRICE_DECIMALS)

    constraints_r, reserves_r, other_r = money(constraints), money(reserves), money(other)
    total_cents = (
        np.rint(constraints_r * 100).astype(np.int64)
        + np.rint(reserves_r * 100).astype(np.int64)
        + np.rint(other_r * 100).astype(np.int64)
    )
    frame = pd.DataFrame(
        {
            "datetime_utc": grid_utc,
            "datetime_local": local,
            "date_local": local_dates.date,
            "hour_local": local.hour.astype("int8"),
            "month": local.month.astype("int8"),
            "is_weekend": local.day_of_week >= 5,
            "is_tariff_holiday": np.isin(local_dates.date, list(holidays)),
            "spot_price_eur_mwh": money(spot),
            "imbalance_price_up_eur_mwh": money(imbalance_up),
            "imbalance_price_down_eur_mwh": money(imbalance_down),
            "ancillary_constraints_eur_mwh": constraints_r,
            "ancillary_reserves_eur_mwh": reserves_r,
            "ancillary_other_eur_mwh": other_r,
            "ancillary_services_cost_eur_mwh": total_cents / 100.0,
            "data_source": source.value,
        }
    )
    frame["is_weekend"] = frame["is_weekend"].astype(bool)
    frame["is_tariff_holiday"] = frame["is_tariff_holiday"].astype(bool)
    return frame.loc[:, list(POWER_COLUMNS)].reset_index(drop=True)


def build_gas_frame(
    days: pd.DatetimeIndex,
    *,
    price: np.ndarray | pd.Series,
    imbalance_buy: np.ndarray,
    imbalance_sell: np.ndarray,
    temperature: np.ndarray,
    weather_source: str,
    source: DataSource,
) -> pd.DataFrame:
    """Dataset diario canónico de gas (día de gas = día de entrega PVB)."""
    temperature = np.round(np.asarray(temperature, dtype="float64"), 2)
    frame = pd.DataFrame(
        {
            "gas_day": days.date,
            "month": days.month.astype("int8"),
            "gas_season": gas_season(days),
            "is_weekend": days.day_of_week >= 5,
            "pvb_da_price_eur_mwh": np.round(np.asarray(price, dtype="float64"), PRICE_DECIMALS),
            "imbalance_buy_price_eur_mwh": np.round(np.asarray(imbalance_buy, dtype="float64"), PRICE_DECIMALS),
            "imbalance_sell_price_eur_mwh": np.round(np.asarray(imbalance_sell, dtype="float64"), PRICE_DECIMALS),
            "temperature_c": temperature,
            "hdd_18c": np.round(np.maximum(HDD_BASE_C - temperature, 0.0), 2),
            "weather_source": weather_source,
            "data_source": source.value,
        }
    )
    frame["is_weekend"] = frame["is_weekend"].astype(bool)
    return frame.loc[:, list(GAS_COLUMNS)].reset_index(drop=True)


# --------------------------------------------------------------------------------------------------
# Calidad de datos
# --------------------------------------------------------------------------------------------------


class QualityReport(BaseModel):
    dataset: str
    rows: int
    passed: bool
    checks: dict[str, bool]
    warnings: list[str] = Field(default_factory=list)
    stats: dict[str, float] = Field(default_factory=dict)


def _raise_if_failed(report: QualityReport) -> QualityReport:
    if not report.passed:
        failed = [name for name, ok in report.checks.items() if not ok]
        raise DataValidationError(f"{report.dataset}: controles fallidos -> {', '.join(failed)}")
    return report


def validate_power_frame(df: pd.DataFrame, year: int, tz: str) -> QualityReport:
    expected_rows = len(hourly_utc_grid(year, tz))
    price_cols = [c for c in POWER_COLUMNS if c.endswith("_eur_mwh")]
    checks: dict[str, bool] = {
        "schema_columns": tuple(df.columns) == POWER_COLUMNS,
        "no_nulls": not bool(df.isna().any().any()),
        "row_count": len(df) == expected_rows,
    }
    warnings: list[str] = []
    if checks["schema_columns"]:
        ts = pd.DatetimeIndex(df["datetime_utc"])
        checks["utc_unique_sorted"] = bool(ts.is_monotonic_increasing and ts.is_unique)
        checks["hourly_continuity"] = bool((pd.Series(ts).diff().dropna().dt.total_seconds() == 3600.0).all())
        checks["price_dtypes_float64"] = all(df[c].dtype == np.float64 for c in price_cols)
        checks["finite_prices"] = bool(np.isfinite(df[price_cols].to_numpy()).all())
        spot = df["spot_price_eur_mwh"]
        checks["spot_within_sdac_limits"] = bool(spot.between(OMIE_PRICE_FLOOR, OMIE_PRICE_CAP).all())
        lo, hi = IMBALANCE_SANITY_RANGE
        checks["imbalance_within_sanity_range"] = bool(
            df["imbalance_price_up_eur_mwh"].between(lo, hi).all() and df["imbalance_price_down_eur_mwh"].between(lo, hi).all()
        )
        component_cents = (
            np.rint(df["ancillary_constraints_eur_mwh"] * 100).astype(np.int64)
            + np.rint(df["ancillary_reserves_eur_mwh"] * 100).astype(np.int64)
            + np.rint(df["ancillary_other_eur_mwh"] * 100).astype(np.int64)
        )
        checks["ancillary_sum_to_the_cent"] = bool(
            (np.rint(df["ancillary_services_cost_eur_mwh"] * 100).astype(np.int64) == component_cents).all()
        )
        ordered = (df["imbalance_price_up_eur_mwh"] <= spot) & (spot <= df["imbalance_price_down_eur_mwh"])
        ordering_ratio = float(ordered.mean())
        if (df["data_source"] == DataSource.SYNTHETIC.value).all():
            checks["dual_imbalance_ordering"] = bool(ordered.all())
        elif ordering_ratio < 0.95:
            warnings.append(f"Ordenación dual up <= spot <= down en solo {ordering_ratio:.1%} de las horas")
        checks["single_source"] = df["data_source"].nunique() == 1
    passed = all(checks.values())
    stats: dict[str, float] = {}
    if passed:
        spot = df["spot_price_eur_mwh"]
        stats = {
            "spot_mean": round(float(spot.mean()), 4),
            "spot_p05": round(float(spot.quantile(0.05)), 4),
            "spot_p95": round(float(spot.quantile(0.95)), 4),
            "zero_or_negative_hours": float((spot <= 0).sum()),
            "imbalance_short_premium_mean": round(float((df["imbalance_price_down_eur_mwh"] - spot).mean()), 4),
            "imbalance_long_discount_mean": round(float((spot - df["imbalance_price_up_eur_mwh"]).mean()), 4),
            "ancillary_mean": round(float(df["ancillary_services_cost_eur_mwh"].mean()), 4),
        }
    return _raise_if_failed(
        QualityReport(dataset="power_market", rows=len(df), passed=passed, checks=checks, warnings=warnings, stats=stats)
    )


def validate_gas_frame(df: pd.DataFrame, year: int, small_adjustment: float = 0.03) -> QualityReport:
    expected_days = len(daily_grid(year))
    checks: dict[str, bool] = {
        "schema_columns": tuple(df.columns) == GAS_COLUMNS,
        "no_nulls": not bool(df.isna().any().any()),
        "row_count": len(df) == expected_days,
    }
    if checks["schema_columns"]:
        days = pd.DatetimeIndex(pd.to_datetime(df["gas_day"]))
        checks["daily_continuity"] = bool(
            days.is_monotonic_increasing
            and days.is_unique
            and (pd.Series(days).diff().dropna().dt.total_seconds() == 86_400.0).all()
        )
        price = df["pvb_da_price_eur_mwh"]
        lo, hi = GAS_SANITY_RANGE
        checks["price_within_sanity_range"] = bool(price.gt(lo).all() and price.le(hi).all())
        checks["imbalance_bracketing"] = bool(
            (df["imbalance_buy_price_eur_mwh"] >= price).all() and (df["imbalance_sell_price_eur_mwh"] <= price).all()
        )
        checks["hdd_non_negative"] = bool((df["hdd_18c"] >= 0).all())
        checks["single_source"] = df["data_source"].nunique() == 1
    passed = all(checks.values())
    stats: dict[str, float] = {}
    if passed:
        price = df["pvb_da_price_eur_mwh"]
        stats = {
            "pvb_mean": round(float(price.mean()), 4),
            "pvb_min": round(float(price.min()), 4),
            "pvb_max": round(float(price.max()), 4),
            "winter_summer_spread": round(
                float(price[df["gas_season"] == "WINTER"].mean() - price[df["gas_season"] == "SUMMER"].mean()), 4
            ),
            "tso_buy_action_days": float(
                (df["imbalance_buy_price_eur_mwh"] > np.round(price * (1.0 + small_adjustment), PRICE_DECIMALS) + 0.01).sum()
            ),
            "tso_sell_action_days": float(
                (df["imbalance_sell_price_eur_mwh"] < np.round(price * (1.0 - small_adjustment), PRICE_DECIMALS) - 0.01).sum()
            ),
        }
    return _raise_if_failed(QualityReport(dataset="gas_market", rows=len(df), passed=passed, checks=checks, stats=stats))


def cross_commodity_correlation(power: pd.DataFrame, gas: pd.DataFrame) -> float:
    """Correlación de Pearson entre el baseload eléctrico diario y el PVB Day-Ahead."""
    baseload = power.groupby("date_local")["spot_price_eur_mwh"].mean()
    joined = pd.concat(
        [baseload.rename("baseload"), gas.set_index("gas_day")["pvb_da_price_eur_mwh"].rename("gas")], axis=1, join="inner"
    )
    return float(joined["baseload"].corr(joined["gas"]))


# --------------------------------------------------------------------------------------------------
# Persistencia (Parquet atómico + manifiesto de linaje)
# --------------------------------------------------------------------------------------------------


def _display_path(path: Path) -> str:
    """Ruta relativa al repositorio cuando es posible (manifiestos portables entre máquinas)."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_parquet_atomic(df: pd.DataFrame, schema: pa.Schema, path: Path, lineage: Mapping[str, Any]) -> str:
    """Escribe con esquema PyArrow estricto (falla si un tipo no casa) y reemplazo atómico."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[b"met.lineage"] = json.dumps(lineage, default=str, ensure_ascii=False).encode("utf-8")
    table = table.replace_schema_metadata(metadata)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        pq.write_table(table, tmp_path, compression="zstd", use_dictionary=["data_source", "gas_season", "weather_source"])
        # mkstemp crea el temporal con permisos 0600: sin esto, un warehouse generado por otro usuario (p. ej. un
        # contenedor de ingesta con otro UID) sería ilegible para la API.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return _sha256(path)


class DatasetLineage(BaseModel):
    dataset: str
    path: str
    source: DataSource
    fallback_reason: str | None = None
    rows: int
    period_start: str
    period_end: str
    imputed_points: dict[str, int] = Field(default_factory=dict)
    sha256: str = ""
    quality: QualityReport


class IngestionResult(BaseModel):
    pipeline_version: str = PIPELINE_VERSION
    generated_at_utc: str
    market_year: int
    random_seed: int
    timezone: str
    power: DatasetLineage
    gas: DatasetLineage
    cross_commodity_correlation: float
    cross_commodity_coherent: bool


# --------------------------------------------------------------------------------------------------
# Orquestador
# --------------------------------------------------------------------------------------------------


_RECOVERABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    requests.RequestException,
    IngestionError,
    DataValidationError,
    ValueError,
    KeyError,
)


class MarketIngestionPipeline:
    """Ejecuta la ingesta de ambas commodities con política de fallback sintético auditada."""

    def __init__(
        self,
        settings: Settings,
        esios_client: EsiosClient | None = None,
        mibgas_client: MibgasClient | None = None,
    ) -> None:
        self.settings = settings
        self._esios_client = esios_client
        self._mibgas_client = mibgas_client
        self._synthetic: SyntheticMarket | None = None

    @property
    def synthetic(self) -> SyntheticMarket:
        if self._synthetic is None:
            s = self.settings
            logger.info("Generando escenario sintético coherente (año=%s, seed=%s)", s.market_year, s.random_seed)
            self._synthetic = SyntheticMarketGenerator(
                year=s.market_year,
                seed=s.random_seed,
                tz=s.timezone,
                reinforced_operation_start=s.reinforced_operation_start,
                small_adjustment=s.mibgas.imbalance_small_adjustment,
            ).generate()
        return self._synthetic

    def _fallback(self, commodity: str, reason: str) -> None:
        if self.settings.strict and not self.settings.mock_data:
            raise IngestionError(f"{commodity}: {reason} (INGESTION_STRICT=true, fallback deshabilitado)")
        logger.warning("%s: usando modo sintético -> %s", commodity, reason)

    def _ingest_power(self) -> tuple[pd.DataFrame, DataSource, str | None, dict[str, int], QualityReport]:
        s = self.settings
        if s.mock_data:
            frame = self.synthetic.power
            return frame, DataSource.SYNTHETIC, "MOCK_DATA=true", {}, validate_power_frame(frame, s.market_year, s.timezone)
        reason: str
        try:
            client = self._esios_client or EsiosClient(s.esios)
            series, imputed = client.fetch_power_market(s.market_year, s.timezone, s.max_imputed_ratio)
            frame = build_power_frame(
                hourly_utc_grid(s.market_year, s.timezone),
                spot=series["spot"],
                imbalance_up=series["up"],
                imbalance_down=series["down"],
                constraints=series["constraints"],
                reserves=series["reserves"],
                other=series["other"],
                source=DataSource.ESIOS,
                tz=s.timezone,
            )
            report = validate_power_frame(frame, s.market_year, s.timezone)
            return frame, DataSource.ESIOS, None, imputed, report
        except _RECOVERABLE_ERRORS as exc:
            reason = f"ESIOS no disponible: {exc}"
        self._fallback("Electricidad", reason)
        frame = self.synthetic.power
        return frame, DataSource.SYNTHETIC, reason, {}, validate_power_frame(frame, s.market_year, s.timezone)

    def _ingest_gas(self) -> tuple[pd.DataFrame, DataSource, str | None, dict[str, int], QualityReport]:
        s = self.settings
        sa = s.mibgas.imbalance_small_adjustment
        if s.mock_data:
            frame = self.synthetic.gas
            return frame, DataSource.SYNTHETIC, "MOCK_DATA=true", {}, validate_gas_frame(frame, s.market_year, sa)
        reason: str
        try:
            client = self._mibgas_client or MibgasClient(s.mibgas)
            price, missing = client.fetch_pvb_day_ahead(s.market_year)
            days = daily_grid(s.market_year)
            price_r = np.round(price.to_numpy(), PRICE_DECIMALS)
            buy, sell = gas_imbalance_prices_nc_bal(price_r, sa)
            frame = build_gas_frame(
                days,
                price=price_r,
                imbalance_buy=buy,
                imbalance_sell=sell,
                temperature=climatological_temperature_c(days),
                weather_source="CLIMATOLOGY",
                source=DataSource.MIBGAS,
            )
            report = validate_gas_frame(frame, s.market_year, sa)
            return frame, DataSource.MIBGAS, None, {"pvb_da_price_forward_filled_days": missing}, report
        except _RECOVERABLE_ERRORS as exc:
            reason = f"MIBGAS no disponible: {exc}"
        self._fallback("Gas", reason)
        frame = self.synthetic.gas
        return frame, DataSource.SYNTHETIC, reason, {}, validate_gas_frame(frame, s.market_year, sa)

    def run(self) -> IngestionResult:
        s = self.settings
        warehouse = s.warehouse_dir
        warehouse.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        power, p_src, p_reason, p_imputed, p_report = self._ingest_power()
        gas, g_src, g_reason, g_imputed, g_report = self._ingest_gas()
        correlation = round(cross_commodity_correlation(power, gas), 4)
        coherent = (p_src == DataSource.SYNTHETIC) == (g_src == DataSource.SYNTHETIC)

        power_lineage = DatasetLineage(
            dataset="power_market",
            path=_display_path(warehouse / POWER_FILENAME),
            source=p_src,
            fallback_reason=p_reason,
            rows=len(power),
            period_start=str(power["datetime_utc"].iloc[0]),
            period_end=str(power["datetime_utc"].iloc[-1]),
            imputed_points=p_imputed,
            quality=p_report,
        )
        gas_lineage = DatasetLineage(
            dataset="gas_market",
            path=_display_path(warehouse / GAS_FILENAME),
            source=g_src,
            fallback_reason=g_reason,
            rows=len(gas),
            period_start=str(gas["gas_day"].iloc[0]),
            period_end=str(gas["gas_day"].iloc[-1]),
            imputed_points=g_imputed,
            quality=g_report,
        )
        common = {"pipeline_version": PIPELINE_VERSION, "generated_at_utc": generated_at, "seed": s.random_seed}
        power_lineage.sha256 = write_parquet_atomic(
            power,
            power_arrow_schema(s.timezone),
            warehouse / POWER_FILENAME,
            {**common, **power_lineage.model_dump(mode="json", exclude={"sha256", "quality"})},
        )
        gas_lineage.sha256 = write_parquet_atomic(
            gas,
            GAS_ARROW_SCHEMA,
            warehouse / GAS_FILENAME,
            {**common, **gas_lineage.model_dump(mode="json", exclude={"sha256", "quality"})},
        )
        result = IngestionResult(
            generated_at_utc=generated_at,
            market_year=s.market_year,
            random_seed=s.random_seed,
            timezone=s.timezone,
            power=power_lineage,
            gas=gas_lineage,
            cross_commodity_correlation=correlation,
            cross_commodity_coherent=coherent,
        )
        manifest_path = warehouse / MANIFEST_FILENAME
        manifest_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result


def load_power_market(warehouse_dir: Path) -> pd.DataFrame:
    """Lectura tipada del dataset eléctrico para capas consumidoras (pricing, back-testing, API)."""
    return pq.read_table(warehouse_dir / POWER_FILENAME).to_pandas()


def load_gas_market(warehouse_dir: Path) -> pd.DataFrame:
    """Lectura tipada del dataset de gas para capas consumidoras."""
    return pq.read_table(warehouse_dir / GAS_FILENAME).to_pandas()


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def _print_summary(result: IngestionResult) -> None:
    p, g = result.power, result.gas
    lines = [
        "",
        "=" * 78,
        f" MET Pricing Engine · Ingesta Multi-Commodity {result.market_year}  (v{result.pipeline_version})",
        "=" * 78,
        f" ELECTRICIDAD  fuente={p.source.value:<10} filas={p.rows:>6}  sha256={p.sha256[:12]}",
        f"   spot medio {p.quality.stats.get('spot_mean', float('nan')):>8.2f} €/MWh | P05 {p.quality.stats.get('spot_p05', float('nan')):>7.2f}"
        f" | P95 {p.quality.stats.get('spot_p95', float('nan')):>7.2f} | horas <= 0: {p.quality.stats.get('zero_or_negative_hours', 0):.0f}",
        f"   desvío corto +{p.quality.stats.get('imbalance_short_premium_mean', float('nan')):.2f} €/MWh | desvío largo "
        f"-{p.quality.stats.get('imbalance_long_discount_mean', float('nan')):.2f} €/MWh | SSAA {p.quality.stats.get('ancillary_mean', float('nan')):.2f} €/MWh",
        f" GAS (PVB DA)  fuente={g.source.value:<10} filas={g.rows:>6}  sha256={g.sha256[:12]}",
        f"   PVB medio {g.quality.stats.get('pvb_mean', float('nan')):>8.2f} €/MWh | rango [{g.quality.stats.get('pvb_min', float('nan')):.2f}, "
        f"{g.quality.stats.get('pvb_max', float('nan')):.2f}] | spread inv-ver {g.quality.stats.get('winter_summer_spread', float('nan')):+.2f}",
        f" Correlación baseload eléctrico vs PVB: {result.cross_commodity_correlation:.3f}"
        f" (coherente={result.cross_commodity_coherent})",
    ]
    if p.fallback_reason:
        lines.append(f" Nota electricidad: {p.fallback_reason}")
    if g.fallback_reason:
        lines.append(f" Nota gas: {g.fallback_reason}")
    lines += [f" Manifiesto: {Path(p.path).parent.as_posix()}/{MANIFEST_FILENAME}", "=" * 78, ""]
    print("\n".join(lines))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingesta Multi-Commodity OMIE/REE ESIOS + MIBGAS PVB")
    parser.add_argument("--year", type=int, default=None, help="Año de mercado (por defecto MARKET_YEAR)")
    parser.add_argument("--seed", type=int, default=None, help="Semilla del modo sintético (por defecto RANDOM_SEED)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="Fuerza el modo sintético")
    mode.add_argument("--live", action="store_true", help="Fuerza la conexión a ESIOS/MIBGAS (fallback salvo --strict)")
    parser.add_argument("--strict", action="store_true", help="Sin fallback: falla si una fuente real no responde")
    parser.add_argument("--warehouse-dir", type=Path, default=None, help="Directorio destino de los Parquet")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    settings = load_settings()
    overrides: dict[str, Any] = {}
    if args.year is not None:
        overrides["market_year"] = args.year
    if args.seed is not None:
        overrides["random_seed"] = args.seed
    if args.mock:
        overrides["mock_data"] = True
    if args.live:
        overrides["mock_data"] = False
    if args.strict:
        overrides["strict"] = True
    if args.warehouse_dir is not None:
        overrides["warehouse_dir"] = args.warehouse_dir.resolve()
    settings = Settings.model_validate({**settings.model_dump(), **overrides})
    try:
        result = MarketIngestionPipeline(settings).run()
    except (IngestionError, DataValidationError) as exc:
        logger.error("Ingesta abortada: %s", exc)
        return 1
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
