"""Parámetros regulados del motor de pricing y registro de procedencia (qué es BOE/CNMC y qué es configurable).

Este módulo es la única fuente de los costes regulados que usan `PowerPricingEngine` y `GasPricingEngine`.
Cada valor pertenece a una de estas tres clases, y `REGULATORY_PARAMETERS` lo declara de forma auditable
(se publica en `GET /api/v1/regulatory/parameters`):

* ``OFICIAL``            Valor publicado en BOE / CNMC, copiado tal cual de la norma citada.
* ``REFERENCIA_OFICIAL`` Valor publicado oficialmente pero usado aquí como aproximación (otro ámbito, otro
                         ejercicio o leído de una fuente secundaria). Se puede sobrescribir por entorno.
* ``CONFIGURABLE``       Hipótesis del comercializador sin valor regulado único. Se fija por variable de entorno.

Vigencias
---------
* Electricidad: ejercicio 2026 (peajes, cargos, pagos por capacidad, retribución de OMIE y del OS).
* Gas: año de gas 2027 (1-oct-2026 a 30-sep-2027). La Resolución CNMC de 2-jun-2026 prorroga los peajes del
  año de gas 2026 y pone a cero el peaje de «otros costes de regasificación».

Convenciones de unidades
------------------------
* Energía en €/MWh (el BOE publica €/kWh: se multiplica por 1.000). Potencia en €/kW·año.
* Los pagos por capacidad y las retribuciones de OMIE y del OS se publican sobre energía en barras de central
  (b.c.): el motor los eleva a contador multiplicando por (1 + coeficiente de pérdidas del periodo).
* Peajes gasistas sin obligación de telemedida: término fijo en €/cliente·año y variable en €/MWh.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Final

from dotenv import load_dotenv

from app.core.config import REPO_ROOT
from app.models.tariffs import GasTariff, PowerTariff

# Los parámetros configurables se leen al importar el módulo: se carga antes el .env de la raíz (sin pisar el entorno).
load_dotenv(REPO_ROOT / ".env", override=False)


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1_000.0) -> float:
    """Lee un parámetro configurable del entorno; un valor ausente, vacío o fuera de rango es un error explícito."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} no es un número válido") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}={value} fuera del rango admitido [{minimum}, {maximum}]")
    return value


# --------------------------------------------------------------------------------------------------
# Electricidad
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerRegulatedTable:
    """Costes regulados por periodo. Energía en €/MWh; potencia en €/kW·año; pérdidas en tanto por uno."""

    toll_energy: tuple[float, ...]
    charge_energy: tuple[float, ...]
    toll_power: tuple[float, ...]
    charge_power: tuple[float, ...]
    capacity_payment: tuple[float, ...]  # €/MWh en barras de central
    losses: tuple[float, ...]

    @property
    def n_periods(self) -> int:
        return len(self.toll_energy)

    @property
    def power_term_eur_kw_year(self) -> float:
        return float(sum(self.toll_power) + sum(self.charge_power))


# OFICIAL. Peajes de transporte y distribución: Resolución CNMC de 18-dic-2025 (vigentes desde 1-ene-2026).
# OFICIAL. Cargos y pagos por capacidad: Orden TED/1524/2025, de 23 de diciembre (BOE-A-2025-26705). Los
#          segmentos tarifarios de cargos 1, 2 y 3 corresponden a los peajes 2.0TD, 3.0TD y 6.1TD. La orden no
#          publica precio de pagos por capacidad para el periodo valle (P3 en 2.0TD, P6 en el resto): es 0.
# OFICIAL. Coeficientes de pérdidas: Circular CNMC 3/2020, art. 11 (la CNMC puede actualizarlos por resolución).
POWER_REGULATED: Final[dict[PowerTariff, PowerRegulatedTable]] = {
    PowerTariff.TD_20: PowerRegulatedTable(
        toll_energy=(33.261, 16.409, 0.077),
        charge_energy=(64.292, 12.858, 3.215),
        toll_power=(23.324952, 0.443770),
        charge_power=(4.379461, 0.281653),
        capacity_payment=(0.800, 0.133, 0.000),
        losses=(0.167, 0.163, 0.180),
    ),
    PowerTariff.TD_30: PowerRegulatedTable(
        toll_energy=(27.511, 12.376, 4.943, 2.627, 0.111, 0.031),
        charge_energy=(35.841, 26.538, 14.336, 7.168, 4.595, 2.867),
        toll_power=(14.935084, 7.894323, 2.502996, 1.907795, 0.535313, 0.535313),
        charge_power=(5.441843, 2.723298, 1.978538, 1.978538, 1.978538, 0.906974),
        capacity_payment=(1.082, 0.500, 0.333, 0.250, 0.250, 0.000),
        losses=(0.166, 0.175, 0.165, 0.165, 0.138, 0.180),
    ),
    PowerTariff.TD_61: PowerRegulatedTable(
        toll_energy=(26.785, 12.281, 5.133, 2.780, 0.120, 0.029),
        charge_energy=(19.489, 14.436, 7.795, 3.898, 2.499, 1.559),
        toll_power=(23.946498, 12.687713, 4.747747, 3.339695, 0.070979, 0.062703),
        charge_power=(5.648870, 2.826996, 2.051434, 2.051434, 2.051434, 0.941478),
        capacity_payment=(0.465, 0.213, 0.142, 0.107, 0.107, 0.000),
        losses=(0.067, 0.068, 0.065, 0.065, 0.043, 0.077),
    ),
}

# OFICIAL. Retribución del operador del mercado: 0,04070 €/MWh que pagan comercializadores y consumidores directos
#          sobre su programa horario final (Orden TED/1524/2025, apartado octavo).
OMIE_FEE_EUR_MWH: Final[float] = 0.04070
# OFICIAL (provisional). Retribución del operador del sistema: 0,20332 €/MWh sobre los programas horarios de 2026
#          (Resolución CNMC de 23-dic-2025, BOE de 31-dic-2025).
OS_FEE_EUR_MWH: Final[float] = 0.20332
# OFICIAL, no repercutido por MWh: cuota fija de 200 €/mes por sujeto de liquidación. Es un coste de estructura de la
# comercializadora (no depende del cliente cotizado), así que queda fuera del build-up y debe cubrirlo el margen.
OS_FIXED_FEE_EUR_MONTH: Final[float] = 200.0
# REFERENCIA_OFICIAL. Fondo Nacional de Eficiencia Energética: no existe un cargo regulado por MWh para el mercado
# libre; la obligación de cada comercializadora depende de sus ventas y de cuánto cubre con CAE. Se toma la
# «Retribución del coste de contribución al FNEE» que la Orden TED/133/2026 (apartado cuarto) fija para el PVPC:
# 0,0024374639 €/kWh. Sobrescribible con PRICING_FNEE_EUR_MWH.
FNEE_EUR_MWH: Final[float] = _env_float("PRICING_FNEE_EUR_MWH", 2.4374639, maximum=50.0)

POWER_REGULATORY_REFERENCE: Final[str] = (
    "Ejercicio 2026. Peajes T&D: Resolución CNMC de 18-dic-2025. Cargos, pagos por capacidad y retribución de OMIE: "
    "Orden TED/1524/2025. Pérdidas: Circular CNMC 3/2020, art. 11. Retribución del OS: Resolución CNMC de 23-dic-2025. "
    "FNEE: referencia del PVPC (Orden TED/133/2026), parámetro configurable."
)

# --------------------------------------------------------------------------------------------------
# Gas natural
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GasRegulatedTable:
    """Peajes y cargos de un nivel RL sin obligación de telemedida (Circular CNMC 6/2020 y RD 1184/2020)."""

    min_mwh: float  # banda de consumo anual (límite inferior, excluido)
    max_mwh: float  # banda de consumo anual (límite superior, incluido)
    local_fixed_eur_year: float  # peaje de redes locales, término fijo
    local_variable_eur_mwh: float  # peaje de redes locales, término variable
    transport_fixed_eur_year: float  # peaje de salida de la red de transporte, término fijo por cliente
    regasification_fixed_eur_year: float  # peaje de otros costes de regasificación
    charge_fixed_eur_year: float  # cargos del sistema gasista

    @property
    def toll_fixed_eur_year(self) -> float:
        """Suma de los términos fijos de peajes (sin cargos), antes de tasa CNMC y cuota GTS."""
        return self.local_fixed_eur_year + self.transport_fixed_eur_year + self.regasification_fixed_eur_year


# OFICIAL. Peajes de redes locales y de salida de la red de transporte: Resolución CNMC de 27-may-2025 (año de gas
#          2026), prorrogados para el año de gas 2027 por la Resolución CNMC de 2-jun-2026 (BOE-A-2026-12173). El
#          término variable del peaje de salida de transporte es 0 para estos niveles.
# OFICIAL. Otros costes de regasificación: 0 desde el 1-oct-2026 (misma Resolución de 2-jun-2026). En el año de gas
#          2026 eran 1,063439 / -0,896167 / -6,127872 / -20,044365 €/cliente·año.
# REFERENCIA_OFICIAL. Cargos: Orden TED/1062/2025, que sigue aplicándose en el año de gas 2027. Las cifras están
#          leídas del informe «Tarifas reguladas» del IDAE (julio 2026), no del BOE: contrastar antes de facturar.
# Nota: el término variable de RL.4 es mayor que el de RL.3. No es una errata de este fichero: así está publicado.
GAS_REGULATED: Final[dict[GasTariff, GasRegulatedTable]] = {
    GasTariff.RL_1: GasRegulatedTable(0.0, 5.0, 23.895429, 15.240, 1.518478, 0.0, 0.29),
    GasTariff.RL_2: GasRegulatedTable(5.0, 15.0, 64.732693, 13.172, 5.326102, 0.0, 0.55),
    GasTariff.RL_3: GasRegulatedTable(15.0, 50.0, 176.302005, 9.782, 13.012555, 0.0, 1.08),
    GasTariff.RL_4: GasRegulatedTable(50.0, 300.0, 430.268266, 12.887, 76.624284, 0.0, 5.43),
}

# OFICIAL. Tasa de la CNMC sobre la facturación de peajes y cargos gasistas: 0,140 % (publicada por MITECO).
GAS_CNMC_FEE_RATIO: Final[float] = 0.00140
# REFERENCIA_OFICIAL. Cuota del Gestor Técnico del Sistema sobre peajes: 1,354 % en el año de gas 2026. El valor del
# año de gas 2027 no está verificado en este repositorio. Sobrescribible con PRICING_GAS_GTS_QUOTA.
GAS_GTS_QUOTA_RATIO: Final[float] = _env_float("PRICING_GAS_GTS_QUOTA", 0.01354, maximum=0.2)
# CONFIGURABLE. Mermas reconocidas entre el PVB y el contador del cliente. Sobrescribible con PRICING_GAS_SHRINKAGE_RATIO.
GAS_SHRINKAGE_RATIO: Final[float] = _env_float("PRICING_GAS_SHRINKAGE_RATIO", 0.005, maximum=0.1)
# REFERENCIA_OFICIAL. FNEE del gas: la obligación de ahorro es proporcional a las ventas de energía final de cada
# sujeto obligado, así que se usa la misma equivalencia por MWh que en electricidad. Sobrescribible con
# PRICING_GAS_FNEE_EUR_MWH.
GAS_FNEE_EUR_MWH: Final[float] = _env_float("PRICING_GAS_FNEE_EUR_MWH", 2.4374639, maximum=50.0)
# OFICIAL. Ajuste menor de las tarifas de desbalance en PVB: 3 % (la Resolución CNMC de 1-jul-2020 lo elevó desde el
# 2,5 %). Es el valor por defecto de GAS_IMBALANCE_SMALL_ADJUSTMENT en `app.core.config`; la ingesta lo aplica al
# construir las tarifas de compra y de venta de Enagás.
GAS_IMBALANCE_SMALL_ADJUSTMENT: Final[float] = 0.03

GAS_REGULATORY_REFERENCE: Final[str] = (
    "Año de gas 2027. Peajes de redes locales y de salida de transporte: Resolución CNMC de 27-may-2025, prorrogada por "
    "la Resolución de 2-jun-2026 (otros costes de regasificación a cero). Cargos: Orden TED/1062/2025 (cifras según "
    "informe IDAE). Tasa CNMC 0,140 %. Cuota GTS, mermas y FNEE: parámetros configurables."
)

# --------------------------------------------------------------------------------------------------
# Registro de procedencia
# --------------------------------------------------------------------------------------------------


class ParameterStatus(str, Enum):
    OFFICIAL = "OFICIAL"
    OFFICIAL_REFERENCE = "REFERENCIA_OFICIAL"
    CONFIGURABLE = "CONFIGURABLE"


@dataclass(frozen=True, slots=True)
class RegulatoryParameter:
    """Una línea del registro: qué valor usa el motor, de dónde sale y si se puede sobrescribir."""

    key: str
    commodity: str
    description: str
    value: str
    unit: str
    status: ParameterStatus
    source: str
    env_var: str | None = None
    note: str | None = None


def _per_period(values: tuple[float, ...], decimals: int = 3) -> str:
    return " / ".join(f"{v:.{decimals}f}" for v in values)


def _by_tariff(attribute: str, decimals: int = 3) -> str:
    return "; ".join(f"{t.value}: {_per_period(getattr(table, attribute), decimals)}" for t, table in POWER_REGULATED.items())


def _by_gas_level(attribute: str, decimals: int) -> str:
    return " / ".join(f"{getattr(table, attribute):.{decimals}f}" for table in GAS_REGULATED.values())


_O, _R, _C = ParameterStatus.OFFICIAL, ParameterStatus.OFFICIAL_REFERENCE, ParameterStatus.CONFIGURABLE

REGULATORY_PARAMETERS: Final[tuple[RegulatoryParameter, ...]] = (
    RegulatoryParameter(
        "power.toll_energy", "POWER", "Peajes de transporte y distribución, término de energía (P1..Pn)",
        _by_tariff("toll_energy"), "€/MWh", _O, "Resolución CNMC de 18-dic-2025 (peajes 2026)",
    ),
    RegulatoryParameter(
        "power.toll_power", "POWER", "Peajes de transporte y distribución, término de potencia (P1..Pn)",
        _by_tariff("toll_power", 6), "€/kW·año", _O, "Resolución CNMC de 18-dic-2025 (peajes 2026)",
    ),
    RegulatoryParameter(
        "power.charge_energy", "POWER", "Cargos del sistema eléctrico, término de energía (P1..Pn)",
        _by_tariff("charge_energy"), "€/MWh", _O, "Orden TED/1524/2025 (BOE-A-2025-26705)",
    ),
    RegulatoryParameter(
        "power.charge_power", "POWER", "Cargos del sistema eléctrico, término de potencia (P1..Pn)",
        _by_tariff("charge_power", 6), "€/kW·año", _O, "Orden TED/1524/2025 (BOE-A-2025-26705)",
    ),
    RegulatoryParameter(
        "power.capacity_payment", "POWER", "Pagos por capacidad (P1..Pn), sobre energía en barras de central",
        _by_tariff("capacity_payment"), "€/MWh b.c.", _O, "Orden TED/1524/2025, apartado tercero",
        note="Segmentos tarifarios 1/2/3 = 2.0TD/3.0TD/6.1TD. El periodo valle no tiene precio publicado: 0.",
    ),
    RegulatoryParameter(
        "power.losses", "POWER", "Coeficientes de pérdidas estándar (P1..Pn)",
        _by_tariff("losses"), "tanto por uno", _O, "Circular CNMC 3/2020, art. 11",
        note="Sustituyen a los del RD 1164/2001. La CNMC puede actualizarlos por resolución: revisar cada ejercicio.",
    ),
    RegulatoryParameter(
        "power.period_calendar", "POWER", "Calendario de periodos horarios P1-P6 (peninsular)",
        "ver assign_periods()", "-", _O, "Circular CNMC 3/2020",
        note="Festivos: solo los nacionales de fecha fija, como establece la Circular.",
    ),
    RegulatoryParameter(
        "power.omie_fee", "POWER", "Retribución del operador del mercado (comercializadores)",
        f"{OMIE_FEE_EUR_MWH:.5f}", "€/MWh b.c.", _O, "Orden TED/1524/2025, apartado octavo",
    ),
    RegulatoryParameter(
        "power.os_fee", "POWER", "Retribución del operador del sistema, cuota variable",
        f"{OS_FEE_EUR_MWH:.5f}", "€/MWh b.c.", _O, "Resolución CNMC de 23-dic-2025 (BOE de 31-dic-2025)",
        note=f"Cuantía provisional. La cuota fija de {OS_FIXED_FEE_EUR_MONTH:.0f} €/mes por sujeto de liquidación no se repercute por MWh.",
    ),
    RegulatoryParameter(
        "power.fnee", "POWER", "Aportación al Fondo Nacional de Eficiencia Energética",
        f"{FNEE_EUR_MWH:.7f}", "€/MWh", _R, "Orden TED/133/2026, apartado cuarto (RFE del PVPC)", "PRICING_FNEE_EUR_MWH",
        "No hay cargo regulado por MWh en mercado libre: depende de las ventas y de la cobertura con CAE de cada comercializadora.",
    ),
    RegulatoryParameter(
        "gas.local_fixed", "GAS", "Peaje de redes locales, término fijo (RL.1 / RL.2 / RL.3 / RL.4)",
        _by_gas_level("local_fixed_eur_year", 6), "€/cliente·año", _O,
        "Resolución CNMC de 27-may-2025, prorrogada por Resolución de 2-jun-2026",
    ),
    RegulatoryParameter(
        "gas.local_variable", "GAS", "Peaje de redes locales, término variable (RL.1 / RL.2 / RL.3 / RL.4)",
        _by_gas_level("local_variable_eur_mwh", 3), "€/MWh", _O,
        "Resolución CNMC de 27-may-2025, prorrogada por Resolución de 2-jun-2026",
        note="RL.4 > RL.3 tal como está publicado.",
    ),
    RegulatoryParameter(
        "gas.transport_fixed", "GAS", "Peaje de salida de la red de transporte, término fijo (RL.1 / RL.2 / RL.3 / RL.4)",
        _by_gas_level("transport_fixed_eur_year", 6), "€/cliente·año", _O,
        "Resolución CNMC de 27-may-2025, prorrogada por Resolución de 2-jun-2026",
    ),
    RegulatoryParameter(
        "gas.regasification_fixed", "GAS", "Peaje de otros costes de regasificación (RL.1 / RL.2 / RL.3 / RL.4)",
        _by_gas_level("regasification_fixed_eur_year", 6), "€/cliente·año", _O,
        "Resolución CNMC de 2-jun-2026 (BOE-A-2026-12173), punto primero",
        note="Valor cero en todas sus modalidades desde el 1-oct-2026.",
    ),
    RegulatoryParameter(
        "gas.charges_fixed", "GAS", "Cargos del sistema gasista (RL.1 / RL.2 / RL.3 / RL.4)",
        _by_gas_level("charge_fixed_eur_year", 2), "€/cliente·año", _R, "Orden TED/1062/2025, vía informe IDAE de julio de 2026",
        note="Cifras leídas del informe del IDAE, no del BOE: contrastar antes de un uso real.",
    ),
    RegulatoryParameter(
        "gas.cnmc_fee", "GAS", "Tasa de la CNMC sobre peajes y cargos",
        f"{100 * GAS_CNMC_FEE_RATIO:.3f}", "%", _O, "Ley 3/2013 (valor publicado por MITECO)",
    ),
    RegulatoryParameter(
        "gas.gts_quota", "GAS", "Cuota del Gestor Técnico del Sistema sobre peajes",
        f"{100 * GAS_GTS_QUOTA_RATIO:.3f}", "%", _R, "Valor del año de gas 2026 (MITECO / IDAE)", "PRICING_GAS_GTS_QUOTA",
        "El valor del año de gas 2027 no está verificado.",
    ),
    RegulatoryParameter(
        "gas.imbalance_small_adjustment", "GAS", "Ajuste menor de las tarifas de desbalance en PVB",
        f"{100 * GAS_IMBALANCE_SMALL_ADJUSTMENT:.1f}", "%", _O,
        "Resolución CNMC de 1-jul-2020 (metodología de tarifas de desbalance; Circular CNMC 2/2020)", "GAS_IMBALANCE_SMALL_ADJUSTMENT",
        "Sustituye al 2,5 % anterior. El registro muestra el valor oficial; el efectivo es el de la ingesta que generó el warehouse.",
    ),
    RegulatoryParameter(
        "gas.fnee", "GAS", "Aportación al Fondo Nacional de Eficiencia Energética",
        f"{GAS_FNEE_EUR_MWH:.7f}", "€/MWh", _R, "Orden TED/133/2026 (equivalencia por MWh del PVPC eléctrico)", "PRICING_GAS_FNEE_EUR_MWH",
        "Aproximación: la obligación es proporcional a las ventas de energía final de cada sujeto obligado.",
    ),
    RegulatoryParameter(
        "gas.shrinkage", "GAS", "Mermas entre el PVB y el contador",
        f"{100 * GAS_SHRINKAGE_RATIO:.2f}", "%", _C, "Hipótesis del comercializador", "PRICING_GAS_SHRINKAGE_RATIO",
    ),
)


def regulatory_register() -> list[dict[str, str | None]]:
    """Registro serializable para la API y para auditoría (una fila por parámetro)."""
    return [{**asdict(parameter), "status": parameter.status.value} for parameter in REGULATORY_PARAMETERS]
