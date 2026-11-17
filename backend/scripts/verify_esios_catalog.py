"""Diagnóstico de las fuentes reales: catálogo e indicadores de REE ESIOS, fichero anual de MIBGAS e ingesta estricta.

Se ejecuta en local, con el token personal en el `.env` de la raíz del repositorio (excluido por `.gitignore`):

    cd backend
    python scripts/verify_esios_catalog.py                 # catálogo + sondeo de cada indicador + MIBGAS
    python scripts/verify_esios_catalog.py --ingest        # además: ingesta --live --strict y validación de Parquet
    python scripts/verify_esios_catalog.py --year 2026 --skip-mibgas

Qué comprueba:
1. Que el token es válido y que cada indicador configurado (spot, desvíos y componentes de servicios de ajuste)
   existe en el catálogo actual de ESIOS. Si alguno no existe o no devuelve datos, propone candidatos del
   catálogo con nombre parecido para actualizar `ESIOS_INDICATOR_*` / `ESIOS_ANCILLARY_*` en `.env`.
2. Que cada indicador devuelve valores para la geografía configurada en dos semanas del año (enero y la última
   semana disponible) y hasta qué fecha hay datos publicados.
3. Que el fichero anual de MIBGAS se descarga y contiene el producto Day-Ahead del PVB.
4. Con `--ingest`: que `market_ingestion --live --strict` descarga el año completo, pasa los controles de calidad
   y escribe ambos Parquet con origen ESIOS y MIBGAS.

El token nunca se imprime: solo se muestran sus cuatro últimos caracteres. Código de salida 0 si todo es correcto.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import EsiosSettings, Settings, load_settings  # noqa: E402
from app.pipelines.market_ingestion import (  # noqa: E402
    DataSource,
    EsiosClient,
    IngestionError,
    MarketIngestionPipeline,
    MibgasClient,
    load_gas_market,
    load_power_market,
)

# Palabras clave para proponer sustitutos si un ID configurado ya no existe o no devuelve datos.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "spot": ("precio mercado spot", "mercado diario", "spot"),
    "imbalance_up": ("desvío", "desvio", "subir"),
    "imbalance_down": ("desvío", "desvio", "bajar"),
    "constraints": ("restricciones",),
    "reserves": ("reserva", "banda secundaria"),
    "other": ("saldo", "factor de potencia", "desvío"),
}


@dataclass(frozen=True, slots=True)
class IndicatorCheck:
    role: str
    indicator_id: int
    in_catalog: bool
    name: str
    values: int
    last_value_utc: str | None
    mean_value: float | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.in_catalog and self.values > 0 and self.error is None


def configured_indicators(settings: EsiosSettings) -> list[tuple[str, int]]:
    """Indicadores que usa la ingesta, con el papel que juega cada uno."""
    pairs = [
        ("spot", settings.indicator_spot),
        ("imbalance_up", settings.indicator_imbalance_up),
        ("imbalance_down", settings.indicator_imbalance_down),
    ]
    pairs += [("constraints", i) for i in settings.ancillary_constraints_ids]
    pairs += [("reserves", i) for i in settings.ancillary_reserves_ids]
    pairs += [("other", i) for i in settings.ancillary_other_ids]
    return pairs


def suggest_replacements(catalog: pd.DataFrame, role: str, limit: int = 5) -> pd.DataFrame:
    """Indicadores del catálogo cuyo nombre contiene todas las palabras clave obligatorias del papel."""
    keywords = ROLE_KEYWORDS[role]
    text = (catalog["name"].astype(str) + " " + catalog["short_name"].astype(str)).str.lower()
    if role in ("imbalance_up", "imbalance_down"):
        direction = keywords[-1]
        mask = text.str.contains("desv") & text.str.contains(direction) & text.str.contains("precio")
    else:
        mask = pd.Series(False, index=catalog.index)
        for keyword in keywords:
            mask |= text.str.contains(keyword, regex=False)
    return catalog[mask].head(limit)


def probe_indicator(
    client: EsiosClient, catalog: pd.DataFrame, role: str, indicator_id: int, windows: Sequence[tuple[datetime, datetime]]
) -> IndicatorCheck:
    """Comprueba que el indicador existe y devuelve valores en las ventanas indicadas."""
    row = catalog[catalog["id"] == indicator_id]
    in_catalog = not row.empty
    name = str(row["name"].iloc[0]) if in_catalog else "(no aparece en el catálogo)"
    collected: list[pd.Series] = []
    error: str | None = None
    for start, end in windows:
        try:
            collected.append(client.fetch_indicator(indicator_id, start, end))
        except IngestionError as exc:
            error = str(exc)
    series = pd.concat(collected).sort_index() if collected else pd.Series(dtype="float64")
    if not series.empty:
        error = None  # basta con que una de las ventanas tenga datos
    return IndicatorCheck(
        role=role,
        indicator_id=indicator_id,
        in_catalog=in_catalog,
        name=name,
        values=int(series.size),
        last_value_utc=series.index.max().isoformat() if not series.empty else None,
        mean_value=round(float(series.mean()), 2) if not series.empty else None,
        error=error,
    )


def probe_windows(year: int, now: datetime) -> list[tuple[datetime, datetime]]:
    """Primera semana de enero y la última semana del año que ya debería estar publicada."""
    january = (datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year, 1, 8, tzinfo=timezone.utc))
    year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    last_end = min(year_end, now.replace(minute=0, second=0, microsecond=0) - timedelta(days=2))
    windows = [january]
    if last_end - timedelta(days=7) > january[1]:
        windows.append((last_end - timedelta(days=7), last_end))
    return windows


def masked(token: str | None) -> str:
    return "(no configurado)" if not token else f"…{token[-4:]} ({len(token)} caracteres)"


def check_esios(settings: Settings, now: datetime, client: EsiosClient | None = None) -> bool:
    print(f"\nREE ESIOS · {settings.esios.base_url} · geo_id={settings.esios.geo_id} (spot), {settings.esios.geo_id_system} (desvíos y ajuste) · token {masked(settings.esios.api_token)}")
    if not settings.esios.api_token:
        print("  ✗ Falta ESIOS_API_TOKEN en el .env de la raíz del repositorio (cp .env.example .env).")
        return False
    client = client or EsiosClient(settings.esios)
    try:
        catalog = client.fetch_catalog()
    except (IngestionError, OSError) as exc:
        print(f"  ✗ No se pudo leer el catálogo: {exc}")
        return False
    print(f"  ✓ Catálogo accesible: {len(catalog)} indicadores")

    windows = probe_windows(settings.market_year, now)
    print(f"  Sondeo de {settings.market_year}: " + " y ".join(f"{a:%d-%m-%Y}→{b:%d-%m-%Y}" for a, b in windows))
    checks = [probe_indicator(client, catalog, role, i, windows) for role, i in configured_indicators(settings.esios)]
    for check in checks:
        mark = "✓" if check.ok else "✗"
        detail = (
            f"{check.values} valores, media {check.mean_value} €/MWh, último {check.last_value_utc}"
            if check.values
            else (check.error or "sin valores")
        )
        print(f"  {mark} {check.indicator_id:>5} {check.role:<15} {check.name[:60]:<60} {detail}")
    failed = [c for c in checks if not c.ok]
    for check in failed:
        candidates = suggest_replacements(catalog, check.role)
        if candidates.empty:
            continue
        print(f"\n  Candidatos del catálogo para sustituir {check.indicator_id} ({check.role}):")
        for _, row in candidates.iterrows():
            print(f"    {int(row['id']):>5}  {str(row['name'])[:90]}")
    if failed:
        print("\n  Actualiza los IDs en .env (ESIOS_INDICATOR_* / ESIOS_ANCILLARY_*) y vuelve a ejecutar el script.")
    return not failed


def check_mibgas(settings: Settings, client: MibgasClient | None = None) -> bool:
    year = settings.market_year
    print(f"\nMIBGAS · producto {settings.mibgas.product_code} · zona {settings.mibgas.area} · año {year}")
    try:
        price, missing = (client or MibgasClient(settings.mibgas)).fetch_pvb_day_ahead(year)
    except (IngestionError, OSError, ValueError) as exc:
        print(f"  ✗ {exc}")
        return False
    print(
        f"  ✓ {len(price)} días; {missing} sin cotización rellenados (fines de semana y festivos); "
        f"PVB medio {price.mean():.2f} €/MWh, rango [{price.min():.2f}, {price.max():.2f}]"
    )
    return True


def run_live_ingestion(
    settings: Settings,
    warehouse_dir: Path | None,
    esios_client: EsiosClient | None = None,
    mibgas_client: MibgasClient | None = None,
) -> bool:
    live = Settings.model_validate(
        {**settings.model_dump(), "mock_data": False, "strict": True, **({"warehouse_dir": warehouse_dir} if warehouse_dir else {})}
    )
    print(f"\nIngesta --live --strict de {live.market_year} → {live.warehouse_dir}")
    try:
        result = MarketIngestionPipeline(live, esios_client=esios_client, mibgas_client=mibgas_client).run()
    except (IngestionError, ValueError) as exc:
        print(f"  ✗ Ingesta abortada: {exc}")
        return False
    power, gas = load_power_market(live.warehouse_dir), load_gas_market(live.warehouse_dir)
    sources = {str(power["data_source"].iloc[0]), str(gas["data_source"].iloc[0])}
    ok = (
        result.power.quality.passed
        and result.gas.quality.passed
        and sources == {DataSource.ESIOS.value, DataSource.MIBGAS.value}
        and int(power.isna().sum().sum()) == 0
        and int(gas.isna().sum().sum()) == 0
    )
    mark = "✓" if ok else "✗"
    print(f"  {mark} power_market.parquet: {len(power)} horas, origen {power['data_source'].iloc[0]}, spot medio {power['spot_price_eur_mwh'].mean():.2f} €/MWh")
    print(f"  {mark} gas_market.parquet: {len(gas)} días, origen {gas['data_source'].iloc[0]}, PVB medio {gas['pvb_da_price_eur_mwh'].mean():.2f} €/MWh")
    print(f"  Horas imputadas por serie: {result.power.imputed_points}")
    return ok


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnóstico de ESIOS, MIBGAS y de la ingesta en vivo")
    parser.add_argument("--year", type=int, default=None, help="Año a comprobar (por defecto MARKET_YEAR)")
    parser.add_argument("--skip-mibgas", action="store_true", help="No comprobar MIBGAS")
    parser.add_argument("--ingest", action="store_true", help="Ejecutar además la ingesta --live --strict y validar los Parquet")
    parser.add_argument("--warehouse-dir", type=Path, default=None, help="Destino de los Parquet con --ingest")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    settings = load_settings()
    if args.year is not None:
        settings = Settings.model_validate({**settings.model_dump(), "market_year": args.year})
    results = [check_esios(settings, datetime.now(timezone.utc))]
    if not args.skip_mibgas:
        results.append(check_mibgas(settings))
    if args.ingest:
        if all(results):
            results.append(run_live_ingestion(settings, args.warehouse_dir.resolve() if args.warehouse_dir else None))
        else:
            print("\nIngesta omitida: corrige primero los fallos anteriores.")
            results.append(False)
    print("\nResultado: " + ("todo correcto" if all(results) else "hay fallos (ver ✗)"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
