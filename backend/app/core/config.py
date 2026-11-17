"""Configuración centralizada y tipada del Enterprise Multi-Commodity Pricing & Imbalance Engine.

Todas las capas (ingesta, pricing, back-testing, API) leen su configuración desde aquí para
garantizar trazabilidad: un único punto de verdad para endpoints, indicadores ESIOS, parámetros
del modo sintético y rutas del Data Warehouse.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

# config.py -> core -> app -> backend -> <repo_root>
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_WAREHOUSE_DIR: Path = REPO_ROOT / "data" / "warehouse"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw.strip())


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw.strip())


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _env_optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    return None if raw is None or raw.strip() == "" else raw.strip()


def _env_int_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(int(part) for part in raw.replace(";", ",").split(",") if part.strip())


def _env_date(name: str, default: date) -> date:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else date.fromisoformat(raw.strip())


class EsiosSettings(BaseModel):
    """Conector REE ESIOS (https://api.esios.ree.es)."""

    model_config = ConfigDict(frozen=True)

    api_token: str | None = None
    base_url: str = "https://api.esios.ree.es"
    geo_id: int = Field(default=3, description="Geografía del spot: 3 = España en ESIOS")
    geo_id_system: int = Field(
        default=8741, description="Geografía de desvíos y servicios de ajuste: 8741 = Península (único publicado)"
    )
    indicator_spot: int = Field(default=600, description="Precio marginal mercado diario (OMIE)")
    indicator_imbalance_up: int = Field(default=686, description="Precio desvíos a subir (posición larga)")
    indicator_imbalance_down: int = Field(default=687, description="Precio desvíos a bajar (posición corta)")
    ancillary_constraints_ids: tuple[int, ...] = Field(
        default=(807, 808, 809), description="Componentes de restricciones técnicas (PBF, TR, intradiario)"
    )
    ancillary_reserves_ids: tuple[int, ...] = Field(
        default=(810, 811), description="Reserva de potencia adicional a subir + banda secundaria"
    )
    ancillary_other_ids: tuple[int, ...] = Field(
        default=(813, 814, 815), description="Saldo desvíos, control factor de potencia, saldo PO 14.6"
    )
    chunk_days: int = Field(default=21, ge=1, le=31)
    timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_token)

    def geo_for(self, indicator_id: int) -> int:
        """ESIOS publica el spot por país y los desvíos/servicios de ajuste solo para el sistema peninsular."""
        return self.geo_id if indicator_id == self.indicator_spot else self.geo_id_system


class MibgasSettings(BaseModel):
    """Conector MIBGAS (ficheros anuales de negociación publicados por el operador del mercado)."""

    model_config = ConfigDict(frozen=True)

    url_template: str = "https://www.mibgas.es/es/file-access/MIBGAS_Data_{year}.xlsx?path=AGNO_{year}/XLS"
    product_code: str = Field(default="GDAES_D+1", description="Producto diario PVB entrega D+1")
    area: str = "PVB"
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    max_gap_days: int = Field(default=4, ge=0, description="Máx. días consecutivos sin cotización (fines de semana/festivos)")
    imbalance_small_adjustment: float = Field(
        default=0.03, ge=0.0, lt=0.5, description="Ajuste menor de las tarifas de desbalance (Resolución CNMC de 1-jul-2020: 3 %)"
    )


class Settings(BaseModel):
    """Configuración raíz del motor."""

    model_config = ConfigDict(frozen=True)

    mock_data: bool = True
    strict: bool = False
    market_year: int = Field(default=2025, ge=2015, le=2100)
    random_seed: int = 42
    timezone: str = "Europe/Madrid"
    warehouse_dir: Path = DEFAULT_WAREHOUSE_DIR
    max_imputed_ratio: float = Field(default=0.01, ge=0.0, le=0.2)
    reinforced_operation_start: date = Field(
        default=date(2025, 4, 28),
        description="Inicio de la operación reforzada de REE tras el cero de tensión ibérico (sube servicios de ajuste)",
    )
    esios: EsiosSettings = EsiosSettings()
    mibgas: MibgasSettings = MibgasSettings()

    @field_validator("warehouse_dir")
    @classmethod
    def _absolute_warehouse(cls, value: Path) -> Path:
        return value if value.is_absolute() else (REPO_ROOT / value).resolve()


def load_settings(env_file: Path | None = None) -> Settings:
    """Carga `.env` (raíz del repo por defecto) y construye la configuración tipada."""
    load_dotenv(env_file if env_file is not None else REPO_ROOT / ".env", override=False)

    esios = EsiosSettings(
        api_token=_env_optional_str("ESIOS_API_TOKEN"),
        base_url=_env_str("ESIOS_BASE_URL", EsiosSettings.model_fields["base_url"].default),
        geo_id=_env_int("ESIOS_GEO_ID", 3),
        geo_id_system=_env_int("ESIOS_GEO_ID_SYSTEM", 8741),
        indicator_spot=_env_int("ESIOS_INDICATOR_SPOT", 600),
        indicator_imbalance_up=_env_int("ESIOS_INDICATOR_IMBALANCE_UP", 686),
        indicator_imbalance_down=_env_int("ESIOS_INDICATOR_IMBALANCE_DOWN", 687),
        ancillary_constraints_ids=_env_int_tuple("ESIOS_ANCILLARY_CONSTRAINTS_IDS", (807, 808, 809)),
        ancillary_reserves_ids=_env_int_tuple("ESIOS_ANCILLARY_RESERVES_IDS", (810, 811)),
        ancillary_other_ids=_env_int_tuple("ESIOS_ANCILLARY_OTHER_IDS", (813, 814, 815)),
        chunk_days=_env_int("ESIOS_CHUNK_DAYS", 21),
        timeout_s=_env_float("ESIOS_TIMEOUT_S", 30.0),
        max_retries=_env_int("ESIOS_MAX_RETRIES", 3),
    )
    mibgas = MibgasSettings(
        url_template=_env_str("MIBGAS_URL_TEMPLATE", MibgasSettings.model_fields["url_template"].default),
        product_code=_env_str("MIBGAS_PRODUCT_CODE", "GDAES_D+1"),
        area=_env_str("MIBGAS_AREA", "PVB"),
        timeout_s=_env_float("MIBGAS_TIMEOUT_S", 60.0),
        max_retries=_env_int("MIBGAS_MAX_RETRIES", 3),
        max_gap_days=_env_int("MIBGAS_MAX_GAP_DAYS", 4),
        imbalance_small_adjustment=_env_float("GAS_IMBALANCE_SMALL_ADJUSTMENT", 0.03),
    )
    return Settings(
        mock_data=_env_bool("MOCK_DATA", True),
        strict=_env_bool("INGESTION_STRICT", False),
        market_year=_env_int("MARKET_YEAR", 2025),
        random_seed=_env_int("RANDOM_SEED", 42),
        timezone=_env_str("MARKET_TIMEZONE", "Europe/Madrid"),
        warehouse_dir=Path(_env_str("WAREHOUSE_DIR", str(DEFAULT_WAREHOUSE_DIR))),
        max_imputed_ratio=_env_float("MAX_IMPUTED_RATIO", 0.01),
        reinforced_operation_start=_env_date("REINFORCED_OPERATION_START", date(2025, 4, 28)),
        esios=esios,
        mibgas=mibgas,
    )
