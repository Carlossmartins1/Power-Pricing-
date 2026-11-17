"use client";

import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ComposedChart, LabelList, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  quotePower,
  type ImbalanceMethod,
  type LoadProfileType,
  type PowerQuoteRequest,
  type PowerQuoteResponse,
  type PowerTariff,
  type ProductStructure,
} from "@/lib/api";
import { annualMwh, type CurveUnit, type LoadCurve } from "@/lib/loadCurve";

import CurveUpload from "./CurveUpload";
import { C, ChartUnit, ControlGroup, DataTable, Kpi, KpiStrip, Legend, Notice, NumberField, Panel, Results, Segmented, Select, Slider, Tip, Workbench, axis, fmt, grid, tipDatum, useApi } from "./ui";

/** Agrupación de las líneas del build-up en seis bloques (orden fijo = orden de apilado y de color). */
export const PRICE_GROUPS = [
  { key: "energy", label: "Energía", color: C.s1, codes: ["OMIE_BASELOAD", "PVB_BASELOAD", "SHAPE", "SEASONAL_SHAPE", "CAP_PAYOFF", "LOSSES", "SHRINKAGE"] },
  { key: "balancing", label: "Ajuste y desvíos", color: C.s2, codes: ["SSAA", "IMBALANCE"] },
  { key: "risk", label: "Primas de riesgo", color: C.s3, codes: ["RISK_PREMIUM", "CAP_PREMIUM"] },
  { key: "tolls", label: "Peajes", color: C.s4, codes: ["TOLLS", "TOLL_VARIABLE", "TOLL_FIXED"] },
  { key: "charges", label: "Cargos y otros regulados", color: C.s5, codes: ["CHARGES", "CAPACITY", "OTHER_REGULATED"] },
  { key: "margin", label: "Margen", color: C.s6, codes: ["MARGIN"] },
] as const;

type GroupKey = (typeof PRICE_GROUPS)[number]["key"];
export type GroupValues = Record<GroupKey, number>;

export function groupComponents(components: Record<string, number>): GroupValues {
  const values = {} as GroupValues;
  for (const group of PRICE_GROUPS) {
    const cents = (group.codes as readonly string[]).reduce((sum, code) => sum + Math.round((components[code] ?? 0) * 100), 0);
    values[group.key] = cents / 100;
  }
  return values;
}

/** Regla de precio: cada bloque ocupa un ancho proporcional a su peso en el precio final. */
export function PriceRuler({ values, total }: { values: GroupValues; total: number }) {
  const visible = PRICE_GROUPS.filter((group) => values[group.key] > 0);
  return (
    <div>
      <div className="flex h-4 gap-[2px]" role="img" aria-label="Composición del precio final por bloques">
        {visible.map((group, index) => (
          <div
            key={group.key}
            title={`${group.label}: ${fmt.n(values[group.key])} €/MWh`}
            className={`${index === 0 ? "rounded-l-[4px]" : ""} ${index === visible.length - 1 ? "rounded-r-[4px]" : ""}`}
            style={{ flexGrow: values[group.key], flexBasis: 0, background: group.color }}
          />
        ))}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-2 sm:grid-cols-3 xl:grid-cols-6">
        {PRICE_GROUPS.map((group) => (
          <div key={group.key}>
            <dt className="flex items-center gap-1.5 text-[12.5px] text-ink-3">
              <span aria-hidden className="inline-block size-2.5 rounded-[2px]" style={{ background: group.color }} />
              {group.label}
            </dt>
            <dd className="mt-0.5 text-[15px] font-semibold text-ink">
              {fmt.n(values[group.key])}
              <span className="ml-1.5 text-[12px] font-normal text-ink-3">{total ? fmt.n((100 * values[group.key]) / total, 0) : 0} %</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const TARIFF_DEFAULTS: Record<PowerTariff, { annual_consumption_mwh: number; contracted_power_kw: number; profile: LoadProfileType }> = {
  "2.0TD": { annual_consumption_mwh: 4, contracted_power_kw: 4.6, profile: "RESIDENTIAL_EVENING" },
  "3.0TD": { annual_consumption_mwh: 500, contracted_power_kw: 120, profile: "COMMERCIAL_DAY" },
  "6.1TD": { annual_consumption_mwh: 5000, contracted_power_kw: 900, profile: "INDUSTRIAL_FLAT" },
};

/**
 * Arquetipos de carga. El `insight` explica el signo del shape cost: en un mercado con mucha fotovoltaica no
 * decide cuánta energía solar usa el cliente, sino a qué horas compra de la red.
 */
const PROFILES: { value: LoadProfileType; label: string; insight: string }[] = [
  { value: "INDUSTRIAL_FLAT", label: "Industrial plano 24/7", insight: "Consume lo mismo todas las horas: paga el baseload y su shape cost es cero." },
  { value: "COMMERCIAL_DAY", label: "Comercial, horario de oficina", insight: "Abre de 8:30 a 19:30, en pleno valle solar: compra por debajo del baseload." },
  { value: "RESIDENTIAL_EVENING", label: "Residencial, pico nocturno", insight: "Su punta es a las 21 h, cuando ya no hay sol y marca precio el gas." },
  {
    value: "SOLAR_PROSUMER",
    label: "Prosumidor con autoconsumo solar",
    insight: "Sus placas cubren el mediodía, que es la energía barata, y de la red solo compra la punta de tarde. Menos MWh, pero los más caros: su shape cost supera al del residencial.",
  },
  {
    value: "SOLAR_ALIGNED_INDUSTRIAL",
    label: "Industrial alineado con el sol",
    insight: "Bombeo solar, desalación o frío flexible: concentra el consumo de 10 a 17 h y captura los precios valle o cero. Shape cost negativo.",
  },
];

const CONSUMPTION_LIMITS = { min: 0.5, max: 500000 } as const;

const PRODUCTS: { value: ProductStructure; label: string; summary: string }[] = [
  { value: "FIXED_SINGLE", label: "Fijo, precio único", summary: "Precio único cerrado para todo el año" },
  { value: "FIXED_TOU", label: "Fijo por periodos", summary: "Precios cerrados por periodo (ver la tabla por periodo) y su precio medio anual" },
  { value: "INDEXED", label: "Indexado a OMIE", summary: "Precio esperado del pass-through de mercado más el fee" },
  { value: "FIXED_WITH_CAP", label: "Indexado con cap", summary: "Precio esperado del indexado con techo de energía" },
];

const INITIAL: PowerQuoteRequest = {
  tariff: "3.0TD",
  profile: "COMMERCIAL_DAY",
  product: "FIXED_SINGLE",
  annual_consumption_mwh: 500,
  forecast_error_pct: 5,
  imbalance_method: "PARAMETRIC",
  monte_carlo_paths: 200,
  risk_aversion: 0.1,
  margin_eur_mwh: 4,
  cap_strike_eur_mwh: 90,
  cap_risk_loading_pct: 10,
  contracted_power_kw: 120,
};

interface PeriodRow extends GroupValues {
  name: string;
  total: number;
  share: number | null;
}

interface HourRow {
  hour: number;
  loadIndex: number;
  priceIndex: number;
  loadKw: number;
  price: number;
}

function QuoteView({ quote, curveName }: { quote: PowerQuoteResponse; curveName: string | null }) {
  const { kpis } = quote;
  const isCustom = quote.profile === "CUSTOM";
  const annual = groupComponents(Object.fromEntries(quote.build_up.map((line) => [line.code, line.value_eur_mwh])));
  const product = PRODUCTS.find((option) => option.value === quote.product);
  const isCap = quote.product === "FIXED_WITH_CAP";
  const isFixed = quote.product === "FIXED_SINGLE" || quote.product === "FIXED_TOU";

  const periodRows: PeriodRow[] = [
    { name: "Media", total: kpis.final_price_eur_mwh, share: null, ...annual },
    ...quote.period_prices.map((period) => ({ name: period.period, total: period.price_eur_mwh, share: period.consumption_share_pct, ...groupComponents(period.components) })),
  ];
  const hourRows: HourRow[] = quote.hourly_profile.map((point) => ({
    hour: point.hour,
    loadIndex: point.client_load_index,
    priceIndex: point.omie_price_index,
    loadKw: point.client_load_mw * 1000,
    price: point.omie_price_eur_mwh,
  }));

  return (
    <>
      {quote.warnings.map((warning) => (
        <Notice key={warning} tone="warning">
          {warning}
        </Notice>
      ))}

      <Panel>
        <div className="grid gap-x-10 gap-y-5 xl:grid-cols-[minmax(0,300px)_minmax(0,1fr)] xl:items-end">
          <div>
            <p className="text-[13px] text-ink-2">Precio final de energía</p>
            <p className="display mt-1 text-[60px] font-semibold leading-none text-paper">
              {fmt.n(kpis.final_price_eur_mwh)}
              <span className="ml-2 text-[17px] font-normal text-ink-2">€/MWh</span>
            </p>
            <p className="mt-2 text-[12.5px] leading-snug text-ink-3">{product?.summary}. Suma exacta al céntimo de las líneas del desglose.</p>
          </div>
          <PriceRuler values={annual} total={kpis.final_price_eur_mwh} />
        </div>
      </Panel>

      <KpiStrip>
        <Kpi label="OMIE baseload" value={fmt.n(kpis.baseload_omie_eur_mwh)} unit="€/MWh" hint={`Media aritmética horaria de ${quote.market_year}`} />
        <Kpi
          label="Coste de perfilado (shape)"
          value={fmt.signed(kpis.shape_cost_eur_mwh)}
          unit="€/MWh"
          hint={`${kpis.shape_cost_eur_mwh > 0 ? "Compra en horas caras" : kpis.shape_cost_eur_mwh < 0 ? "Compra en horas baratas" : "Perfil plano"}: OMIE ponderado ${fmt.n(kpis.weighted_omie_eur_mwh)}, factor de forma ${fmt.n(kpis.shape_factor)}`}
        />
        <Kpi label="Servicios de ajuste REE" value={fmt.n(kpis.ancillary_cost_eur_mwh)} unit="€/MWh" hint="Ponderados por la curva del cliente" />
        <Kpi
          label="Coste de desvíos"
          value={fmt.n(kpis.imbalance_cost_eur_mwh)}
          unit="€/MWh"
          hint={kpis.imbalance_cost_p95_eur_mwh !== null ? `Monte Carlo; P95 ${fmt.n(kpis.imbalance_cost_p95_eur_mwh)} €/MWh` : "Valor esperado paramétrico"}
        />
        {isCap ? (
          <Kpi
            label={`Prima del cap a ${fmt.n(kpis.cap_strike_eur_mwh ?? 0, 0)} €/MWh`}
            value={fmt.n(kpis.cap_option_premium_eur_mwh ?? 0)}
            unit="€/MWh"
            hint={`${fmt.n(kpis.cap_hours_in_the_money ?? 0, 0)} horas del año por encima del techo`}
          />
        ) : (
          <Kpi label="Prima de riesgo" value={fmt.n(kpis.risk_premium_eur_mwh)} unit="€/MWh" hint={isFixed ? "Dispersión P95−P50 del coste diario" : "El cliente asume el riesgo de precio"} />
        )}
        <Kpi label="Peajes, cargos y regulados" value={fmt.n(kpis.regulated_cost_eur_mwh)} unit="€/MWh" hint="Término de energía, repercutido a coste" />
        <Kpi label="Factura anual de energía" value={fmt.eur(kpis.annual_energy_bill_eur)} hint={`${fmt.n(quote.annual_consumption_mwh, 0)} MWh al año`} />
        <Kpi label="Margen bruto esperado" value={fmt.eur(kpis.expected_gross_margin_eur)} hint={`${fmt.n(kpis.margin_eur_mwh)} €/MWh de ${isFixed ? "margen" : "fee"}`} />
      </KpiStrip>

      <div className="grid gap-5 2xl:grid-cols-2">
        <Panel
          title="Composición de la tarifa por periodo"
          note={quote.product === "FIXED_TOU" ? "Precios de contrato de cada periodo y su media ponderada por consumo." : "Coste de servir cada periodo; el contrato factura la media."}
          aside={<Legend items={PRICE_GROUPS.map((group) => ({ label: group.label, color: group.color }))} />}
        >
          <ChartUnit>€/MWh</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={periodRows} margin={{ top: 16, right: 4, bottom: 0, left: 0 }} barCategoryGap="24%" stackOffset="sign">
              <CartesianGrid {...grid} />
              <XAxis dataKey="name" {...axis} />
              <YAxis {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ fill: C.raised, opacity: 0.5 }}
                content={(props) => {
                  const row = tipDatum<PeriodRow>(props);
                  if (!row) return null;
                  return (
                    <Tip
                      title={row.name === "Media" ? "Media anual" : `Periodo ${row.name}`}
                      rows={[...PRICE_GROUPS.map((group) => ({ label: group.label, value: fmt.n(row[group.key]), color: group.color })), { label: "Total", value: `${fmt.n(row.total)} €/MWh` }]}
                      footer={row.share !== null ? `${fmt.n(row.share, 1)} % del consumo anual` : undefined}
                    />
                  );
                }}
              />
              {PRICE_GROUPS.map((group, index) => (
                <Bar key={group.key} dataKey={group.key} stackId="price" fill={group.color} stroke={C.panel} strokeWidth={2} isAnimationActive={false} radius={index === PRICE_GROUPS.length - 1 ? [4, 4, 0, 0] : 0}>
                  {index === PRICE_GROUPS.length - 1 && <LabelList dataKey="total" position="top" fill={C.ink2} fontSize={12} formatter={(value: unknown) => fmt.n(Number(value), 0)} />}
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel
          title={isCustom ? "Tu curva frente a OMIE" : "Curva horaria del cliente frente a OMIE"}
          note={`${isCustom && curveName ? `Día medio de «${curveName}». ` : ""}Ambas curvas en índice (media del día = 100) para leerlas en un solo eje: consumir por encima de 100 cuando OMIE también lo está encarece el perfil.`}
          aside={
            <Legend
              items={[
                { label: isCustom ? "Tu curva" : "Consumo del cliente", color: C.s1 },
                { label: "Precio OMIE", color: C.s2, shape: "line" },
              ]}
            />
          }
        >
          <ChartUnit>Índice</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={hourRows} margin={{ top: 16, right: 8, bottom: 0, left: 0 }} barCategoryGap="18%">
              <CartesianGrid {...grid} />
              <XAxis dataKey="hour" {...axis} interval={2} tickFormatter={(hour: number) => `${hour} h`} />
              <YAxis {...axis} axisLine={false} width={44} />
              <ReferenceLine y={100} stroke={C.neutral} strokeDasharray="4 4" />
              <Tooltip
                cursor={{ fill: C.raised, opacity: 0.5 }}
                content={(props) => {
                  const row = tipDatum<HourRow>(props);
                  if (!row) return null;
                  const load = row.loadKw >= 1000 ? `${fmt.n(row.loadKw / 1000)} MW` : `${fmt.n(row.loadKw, 1)} kW`;
                  return (
                    <Tip
                      title={`De ${row.hour}:00 a ${row.hour + 1}:00`}
                      rows={[
                        { label: "Consumo medio", value: `${load} (índice ${fmt.n(row.loadIndex, 0)})`, color: C.s1 },
                        { label: "OMIE medio", value: `${fmt.n(row.price)} €/MWh (índice ${fmt.n(row.priceIndex, 0)})`, color: C.s2 },
                      ]}
                    />
                  );
                }}
              />
              <Bar dataKey="loadIndex" fill={C.s1} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              <Line dataKey="priceIndex" stroke={C.s2} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: C.panel, strokeWidth: 2 }} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </Panel>
      </div>

      <div className="grid gap-5 2xl:grid-cols-2">
        <Panel title="Desglose auditable" note="Cada línea se redondea al céntimo y el total es su suma exacta.">
          <DataTable
            head={["Componente", "Tratamiento", "€/MWh", "% del precio"]}
            rows={quote.build_up.map((line) => [line.label, line.pass_through ? "Repercutido" : "Cerrado", fmt.signed(line.value_eur_mwh).replace("+", ""), `${fmt.n((100 * line.value_eur_mwh) / kpis.final_price_eur_mwh, 1)} %`])}
            foot={["Precio final", "", fmt.n(kpis.final_price_eur_mwh), "100,0 %"]}
          />
        </Panel>
        <Panel title="Precios por periodo tarifario, €/MWh" note="Calendario peninsular de la Circular CNMC 3/2020; fines de semana y festivos nacionales van al último periodo.">
          <DataTable
            head={["Periodo", "Horas", "MWh", "% consumo", "OMIE pond.", "Precio"]}
            rows={quote.period_prices.map((period) => [
              period.period,
              fmt.n(period.hours, 0),
              fmt.n(period.consumption_mwh, period.consumption_mwh < 100 ? 2 : 0),
              `${fmt.n(period.consumption_share_pct, 1)} %`,
              fmt.n(period.omie_weighted_eur_mwh),
              <span key="price" className="font-semibold text-ink">{fmt.n(period.price_eur_mwh)}</span>,
            ])}
            foot={["Media ponderada", fmt.n(quote.period_prices.reduce((sum, period) => sum + period.hours, 0), 0), fmt.n(quote.annual_consumption_mwh, quote.annual_consumption_mwh < 100 ? 2 : 0), "100,0 %", fmt.n(kpis.weighted_omie_eur_mwh), fmt.n(quote.tou_weighted_average_eur_mwh)]}
          />
          {Math.abs(quote.tou_reconciliation_diff_eur_mwh) >= 0.005 && (
            <p className="mt-3 text-[12.5px] text-ink-3">
              La media ponderada de los precios por periodo difiere del precio anual en {fmt.signed(quote.tou_reconciliation_diff_eur_mwh)} €/MWh: cada periodo se redondea al céntimo por separado.
            </p>
          )}
          {kpis.power_term_eur_year !== null && (
            <p className="mt-3 text-[12.5px] text-ink-3">
              Término de potencia regulado (peajes y cargos): <span className="font-semibold text-ink-2">{fmt.eur(kpis.power_term_eur_year)} al año</span>, facturado aparte del precio de energía.
            </p>
          )}
        </Panel>
      </div>
      <p className="text-[12px] leading-snug text-ink-3">{quote.regulatory_reference}</p>
    </>
  );
}

export default function PowerPricerTab() {
  const [request, setRequest] = useState<PowerQuoteRequest>(INITIAL);
  const [curve, setCurve] = useState<LoadCurve | null>(null);
  const [unit, setUnit] = useState<CurveUnit>("kWh");
  // Con una curva cargada, su forma sustituye al arquetipo; al quitarla se vuelve al arquetipo elegido.
  const effective = useMemo<PowerQuoteRequest>(() => (curve ? { ...request, profile: "CUSTOM", custom_profile: curve.values } : request), [request, curve]);
  const state = useApi(quotePower, effective);
  const patch = (changes: Partial<PowerQuoteRequest>) => setRequest((current) => ({ ...current, ...changes }));
  const profile = PROFILES.find((option) => option.value === request.profile);
  const fileVolume = (loaded: LoadCurve, fileUnit: CurveUnit) => annualMwh(loaded, fileUnit, CONSUMPTION_LIMITS.min, CONSUMPTION_LIMITS.max);

  return (
    <Workbench
      controls={
        <>
          <ControlGroup title="Suministro">
            <Segmented<PowerTariff>
              label="Tarifa de acceso"
              value={request.tariff}
              onChange={(tariff) => patch(curve ? { tariff, contracted_power_kw: TARIFF_DEFAULTS[tariff].contracted_power_kw } : { tariff, ...TARIFF_DEFAULTS[tariff] })}
              options={[
                { value: "2.0TD", label: "2.0TD" },
                { value: "3.0TD", label: "3.0TD" },
                { value: "6.1TD", label: "6.1TD" },
              ]}
            />
            {!curve && <Select<LoadProfileType> label="Perfil de carga" value={request.profile} onChange={(next) => patch({ profile: next })} options={PROFILES} hint={profile?.insight} />}
            <CurveUpload
              curve={curve}
              unit={unit}
              year={state.data?.market_year ?? null}
              onLoad={(loaded) => {
                setCurve(loaded);
                patch({ annual_consumption_mwh: fileVolume(loaded, unit) });
              }}
              onUnitChange={(next) => {
                setUnit(next);
                if (curve) patch({ annual_consumption_mwh: fileVolume(curve, next) });
              }}
              onClear={() => setCurve(null)}
            />
            <NumberField
              label="Consumo anual"
              unit="MWh"
              value={request.annual_consumption_mwh}
              onChange={(annual_consumption_mwh) => patch({ annual_consumption_mwh })}
              min={CONSUMPTION_LIMITS.min}
              max={CONSUMPTION_LIMITS.max}
              step={request.annual_consumption_mwh < 20 ? 0.5 : 10}
              hint={curve ? "Tomado del total del fichero. Si lo cambias, la curva se reescala manteniendo su forma." : undefined}
            />
            <NumberField label="Potencia contratada" unit="kW" value={request.contracted_power_kw ?? 0} onChange={(contracted_power_kw) => patch({ contracted_power_kw })} min={1} max={500000} step={request.tariff === "2.0TD" ? 0.1 : 10} />
          </ControlGroup>
          <ControlGroup title="Producto">
            <Select<ProductStructure> label="Estructura de precio" value={request.product} onChange={(product) => patch({ product })} options={PRODUCTS} />
            {request.product === "FIXED_WITH_CAP" && (
              <Slider label="Techo de energía (cap)" value={request.cap_strike_eur_mwh} onChange={(cap_strike_eur_mwh) => patch({ cap_strike_eur_mwh })} min={40} max={200} step={5} display={`${request.cap_strike_eur_mwh} €/MWh`} hint="La prima es el valor histórico de la call horaria más un 10 % de recargo." />
            )}
            <Slider label={request.product === "INDEXED" || request.product === "FIXED_WITH_CAP" ? "Fee de gestión" : "Margen comercial"} value={request.margin_eur_mwh} onChange={(margin_eur_mwh) => patch({ margin_eur_mwh })} min={0} max={15} step={0.25} display={`${fmt.n(request.margin_eur_mwh)} €/MWh`} />
          </ControlGroup>
          <ControlGroup title="Riesgo">
            <Slider label="Error de previsión horario" value={request.forecast_error_pct} onChange={(forecast_error_pct) => patch({ forecast_error_pct })} min={0} max={25} step={0.5} display={`${fmt.n(request.forecast_error_pct, 1)} %`} hint="Desviación típica del consumo real frente al previsto." />
            <Segmented<ImbalanceMethod>
              label="Cálculo de desvíos"
              value={request.imbalance_method}
              onChange={(imbalance_method) => patch({ imbalance_method })}
              options={[
                { value: "PARAMETRIC", label: "Paramétrico" },
                { value: "MONTE_CARLO", label: "Monte Carlo" },
              ]}
            />
            <Slider label="Aversión al riesgo" value={request.risk_aversion} onChange={(risk_aversion) => patch({ risk_aversion })} min={0} max={0.5} step={0.01} display={`${fmt.n(request.risk_aversion * 100, 0)} %`} hint="Parte de la dispersión P95−P50 que se carga como prima en los fijos." />
          </ControlGroup>
        </>
      }
    >
      <Results state={state}>{(quote) => <QuoteView quote={quote} curveName={curve?.fileName ?? null} />}</Results>
    </Workbench>
  );
}
