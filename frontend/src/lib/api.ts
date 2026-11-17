/**
 * Cliente tipado de la API FastAPI (`/api/v1`). Los tipos reflejan los modelos Pydantic del backend.
 * La URL base se configura con NEXT_PUBLIC_API_URL (por defecto http://localhost:8000).
 */

export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

// ---------------------------------------------------------------------------------------------
// Tipos comunes
// ---------------------------------------------------------------------------------------------

export type ComponentCategory = "ENERGY" | "BALANCING" | "RISK" | "REGULATED" | "MARGIN";

export interface BuildUpComponent {
  code: string;
  label: string;
  category: ComponentCategory;
  value_eur_mwh: number;
  pass_through: boolean;
}

// ---------------------------------------------------------------------------------------------
// Electricidad
// ---------------------------------------------------------------------------------------------

export type PowerTariff = "2.0TD" | "3.0TD" | "6.1TD";
export type LoadProfileType = "INDUSTRIAL_FLAT" | "COMMERCIAL_DAY" | "RESIDENTIAL_EVENING" | "SOLAR_PROSUMER" | "SOLAR_ALIGNED_INDUSTRIAL" | "CUSTOM";
export type ProductStructure = "FIXED_SINGLE" | "FIXED_TOU" | "INDEXED" | "FIXED_WITH_CAP";
export type ImbalanceMethod = "PARAMETRIC" | "MONTE_CARLO";

export interface PowerQuoteRequest {
  tariff: PowerTariff;
  profile: LoadProfileType;
  product: ProductStructure;
  annual_consumption_mwh: number;
  custom_profile?: number[] | null;
  forecast_error_pct: number;
  imbalance_method: ImbalanceMethod;
  monte_carlo_paths?: number;
  random_seed?: number;
  risk_aversion: number;
  margin_eur_mwh: number;
  cap_strike_eur_mwh: number;
  cap_risk_loading_pct?: number;
  contracted_power_kw?: number | null;
}

export interface PeriodPrice {
  period: string;
  hours: number;
  consumption_mwh: number;
  consumption_share_pct: number;
  omie_weighted_eur_mwh: number;
  price_eur_mwh: number;
  components: Record<string, number>;
}

export interface HourlyProfilePoint {
  hour: number;
  client_load_mw: number;
  client_load_index: number;
  omie_price_eur_mwh: number;
  omie_price_index: number;
}

export interface PowerQuoteKpis {
  final_price_eur_mwh: number;
  baseload_omie_eur_mwh: number;
  weighted_omie_eur_mwh: number;
  shape_cost_eur_mwh: number;
  shape_factor: number;
  ancillary_cost_eur_mwh: number;
  imbalance_cost_eur_mwh: number;
  imbalance_cost_p95_eur_mwh: number | null;
  risk_premium_eur_mwh: number;
  regulated_cost_eur_mwh: number;
  margin_eur_mwh: number;
  cap_strike_eur_mwh: number | null;
  cap_option_premium_eur_mwh: number | null;
  cap_hours_in_the_money: number | null;
  annual_energy_bill_eur: number;
  expected_gross_margin_eur: number;
  power_term_eur_year: number | null;
}

export interface PowerQuoteResponse {
  tariff: PowerTariff;
  profile: LoadProfileType;
  product: ProductStructure;
  annual_consumption_mwh: number;
  market_year: number;
  data_source: string;
  kpis: PowerQuoteKpis;
  build_up: BuildUpComponent[];
  period_prices: PeriodPrice[];
  tou_weighted_average_eur_mwh: number;
  tou_reconciliation_diff_eur_mwh: number;
  hourly_profile: HourlyProfilePoint[];
  regulatory_reference: string;
  warnings: string[];
}

// ---------------------------------------------------------------------------------------------
// Gas natural
// ---------------------------------------------------------------------------------------------

export type GasTariff = "RL.1" | "RL.2" | "RL.3" | "RL.4";
export type GasProfileType = "RESIDENTIAL_HEATING" | "COMMERCIAL" | "INDUSTRIAL_PROCESS";
export type GasProduct = "FIXED" | "INDEXED";

export interface GasQuoteRequest {
  tariff: GasTariff;
  profile: GasProfileType;
  product: GasProduct;
  annual_consumption_mwh: number;
  forecast_error_pct: number;
  risk_aversion: number;
  margin_eur_mwh: number;
}

export interface GasMonthlyPoint {
  month: number;
  consumption_mwh: number;
  consumption_share_pct: number;
  pvb_avg_eur_mwh: number;
  hdd: number;
}

export interface GasQuoteKpis {
  final_price_eur_mwh: number;
  baseload_pvb_eur_mwh: number;
  weighted_pvb_eur_mwh: number;
  seasonal_shape_cost_eur_mwh: number;
  shape_factor: number;
  imbalance_cost_eur_mwh: number;
  risk_premium_eur_mwh: number;
  regulated_cost_eur_mwh: number;
  margin_eur_mwh: number;
  winter_consumption_share_pct: number;
  annual_bill_eur: number;
  expected_gross_margin_eur: number;
}

export interface GasQuoteResponse {
  tariff: GasTariff;
  profile: GasProfileType;
  product: GasProduct;
  annual_consumption_mwh: number;
  market_year: number;
  data_source: string;
  kpis: GasQuoteKpis;
  build_up: BuildUpComponent[];
  monthly: GasMonthlyPoint[];
  regulatory_reference: string;
  warnings: string[];
}

// ---------------------------------------------------------------------------------------------
// Cartera y back-test
// ---------------------------------------------------------------------------------------------

export interface StressScenario {
  volume_shock_pct: number;
  shape_shift_pct: number;
  imbalance_multiplier: number;
  unhedged_ratio: number;
  wholesale_price_shock_pct: number;
}

export interface BacktestRequest {
  n_contracts: number;
  seed: number;
  stress: StressScenario;
  margin_threshold_eur_mwh: number;
  max_alerts: number;
}

export interface ContractResult {
  contract_id: string;
  commodity: "POWER" | "GAS";
  tariff: string;
  profile: string;
  product: "FIXED" | "INDEXED";
  signed_months_ago: number;
  contracted_mwh: number;
  realized_mwh: number;
  contract_price_eur_mwh: number;
  ex_ante_margin_eur: number;
  ex_post_margin_eur: number;
  volume_effect_eur: number;
  shape_effect_eur: number;
  wholesale_hedging_effect_eur: number;
  imbalance_effect_eur: number;
  ex_ante_unit_margin_eur_mwh: number;
  realized_unit_margin_eur_mwh: number;
  alert: "OK" | "REPRICE_REQUIRED";
}

export interface WaterfallStep {
  key: string;
  label: string;
  kind: "total" | "effect";
  value_eur: number;
  start_eur: number;
  end_eur: number;
}

export interface SegmentSummary {
  tariff: string;
  commodity: "POWER" | "GAS";
  contracts: number;
  contracted_mwh: number;
  realized_mwh: number;
  ex_ante_margin_eur: number;
  ex_post_margin_eur: number;
  realized_unit_margin_eur_mwh: number;
  contracts_at_risk: number;
}

export interface BacktestKpis {
  contracts: number;
  contracted_mwh: number;
  realized_mwh: number;
  ex_ante_margin_eur: number;
  ex_post_margin_eur: number;
  margin_delta_eur: number;
  margin_delta_pct: number;
  ex_ante_unit_margin_eur_mwh: number;
  ex_post_unit_margin_eur_mwh: number;
  contracts_at_risk: number;
  at_risk_volume_share_pct: number;
  open_position_mwh: number;
}

export interface BacktestResponse {
  market_year: number;
  data_source: string;
  stress: StressScenario;
  margin_threshold_eur_mwh: number;
  kpis: BacktestKpis;
  attribution: Record<string, number>;
  waterfall: WaterfallStep[];
  reconciliation_error_eur: number;
  segments: SegmentSummary[];
  at_risk_contracts: ContractResult[];
  assumptions: string[];
}

// ---------------------------------------------------------------------------------------------
// Salud e inteligencia de mercado
// ---------------------------------------------------------------------------------------------

export interface DatasetHealth {
  rows: number;
  source: string;
  sha256: string | null;
  quality_passed: boolean | null;
  fallback_reason: string | null;
}

export interface HealthResponse {
  status: string;
  api_version: string;
  loaded_at_utc: string;
  market_year: number;
  manifest_found: boolean;
  pipeline_version: string | null;
  generated_at_utc: string | null;
  cross_commodity_correlation: number | null;
  power: DatasetHealth;
  gas: DatasetHealth;
}

export interface HourlyMarketPoint {
  hour: number;
  spot_mean: number;
  spot_winter: number;
  spot_spring: number;
  spot_summer: number;
  spot_autumn: number;
  short_premium_mean: number;
  short_premium_p90: number;
  long_discount_mean: number;
  long_discount_p90: number;
  zero_price_share_pct: number;
}

export interface MonthlyMarketPoint {
  month: number;
  baseload_eur_mwh: number;
  pvb_eur_mwh: number;
  ancillary_total_eur_mwh: number;
  ancillary_constraints_eur_mwh: number;
  ancillary_reserves_eur_mwh: number;
  ancillary_other_eur_mwh: number;
  zero_or_negative_hours: number;
}

export interface CorrelationPoint {
  day: string;
  season: "WINTER" | "SUMMER";
  baseload_eur_mwh: number;
  pvb_eur_mwh: number;
}

export interface MarketSummary {
  baseload_eur_mwh: number;
  solar_hours_price_eur_mwh: number;
  evening_peak_price_eur_mwh: number;
  duck_curve_spread_eur_mwh: number;
  zero_or_negative_hours: number;
  short_premium_mean_eur_mwh: number;
  long_discount_mean_eur_mwh: number;
  ancillary_mean_eur_mwh: number;
  pvb_mean_eur_mwh: number;
  pvb_winter_summer_spread_eur_mwh: number;
  power_gas_correlation: number;
  power_gas_correlation_winter: number;
  power_gas_correlation_summer: number;
}

export interface ArchetypeShapePoint {
  profile: LoadProfileType;
  captured_price_eur_mwh: number;
  shape_cost_eur_mwh: number;
  shape_factor: number;
  solar_window_share_pct: number;
}

export interface MarketIntelligenceResponse {
  market_year: number;
  data_source: string;
  summary: MarketSummary;
  hourly: HourlyMarketPoint[];
  monthly: MonthlyMarketPoint[];
  correlation: CorrelationPoint[];
  archetype_shapes: ArchetypeShapePoint[];
}

// ---------------------------------------------------------------------------------------------
// Transporte
// ---------------------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

function describeError(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return (detail as ValidationIssue[])
      .map((issue) => `${(issue.loc ?? []).filter((part) => part !== "body").join(".")}: ${issue.msg ?? "valor no válido"}`)
      .join("; ");
  }
  return `La API respondió con el estado ${status}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/v1${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", Accept: "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(`No se puede conectar con la API en ${API_BASE_URL}. Arranca el backend con uvicorn y vuelve a intentarlo.`, null);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(describeError(body, response.status), response.status);
  return body as T;
}

const post = <TReq, TRes>(path: string) => (payload: TReq, signal?: AbortSignal) =>
  request<TRes>(path, { method: "POST", body: JSON.stringify(payload), signal });

export const getHealth = (signal?: AbortSignal) => request<HealthResponse>("/health", { signal });
export const getMarketIntelligence = (signal?: AbortSignal) => request<MarketIntelligenceResponse>("/market/intelligence", { signal });
export const quotePower = post<PowerQuoteRequest, PowerQuoteResponse>("/price-quote/power");
export const quoteGas = post<GasQuoteRequest, GasQuoteResponse>("/price-quote/gas");
export const runBacktest = post<BacktestRequest, BacktestResponse>("/portfolio/backtest");
