"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { quoteGas, type GasProduct, type GasProfileType, type GasQuoteRequest, type GasQuoteResponse, type GasTariff } from "@/lib/api";

import { PriceRuler, groupComponents } from "./PowerPricerTab";
import { C, ControlGroup, DataTable, Kpi, KpiStrip, MONTHS, Notice, NumberField, Panel, Results, Segmented, Select, Slider, Tip, Workbench, axis, fmt, grid, tipDatum, useApi } from "./ui";

const TARIFF_VOLUMES: Record<GasTariff, { typical: number; band: string }> = {
  "RL.1": { typical: 3, band: "hasta 5 MWh/año" },
  "RL.2": { typical: 9, band: "de 5 a 15 MWh/año" },
  "RL.3": { typical: 30, band: "de 15 a 50 MWh/año" },
  "RL.4": { typical: 150, band: "de 50 a 300 MWh/año" },
};

const PROFILES: { value: GasProfileType; label: string }[] = [
  { value: "RESIDENTIAL_HEATING", label: "Residencial con calefacción" },
  { value: "COMMERCIAL", label: "Comercial y servicios" },
  { value: "INDUSTRIAL_PROCESS", label: "Proceso industrial" },
];

const INITIAL: GasQuoteRequest = {
  tariff: "RL.2",
  profile: "RESIDENTIAL_HEATING",
  product: "FIXED",
  annual_consumption_mwh: 9,
  forecast_error_pct: 8,
  risk_aversion: 0.1,
  margin_eur_mwh: 3,
};

interface MonthRow {
  month: string;
  consumption: number;
  share: number;
  pvb: number;
  hdd: number;
}

function monthTip(props: unknown) {
  const row = tipDatum<MonthRow>(props);
  if (!row) return null;
  return (
    <Tip
      title={row.month}
      rows={[
        { label: "Consumo", value: `${fmt.n(row.consumption)} MWh (${fmt.n(row.share, 1)} %)`, color: C.s1 },
        { label: "PVB medio", value: `${fmt.n(row.pvb)} €/MWh`, color: C.s2 },
        { label: "Grados-día (HDD)", value: fmt.n(row.hdd, 0) },
      ]}
    />
  );
}

function QuoteView({ quote }: { quote: GasQuoteResponse }) {
  const { kpis } = quote;
  const values = groupComponents(Object.fromEntries(quote.build_up.map((line) => [line.code, line.value_eur_mwh])));
  const rows: MonthRow[] = quote.monthly.map((point) => ({
    month: MONTHS[point.month - 1],
    consumption: point.consumption_mwh,
    share: point.consumption_share_pct,
    pvb: point.pvb_avg_eur_mwh,
    hdd: point.hdd,
  }));
  const isFixed = quote.product === "FIXED";

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
            <p className="text-[13px] text-ink-2">Precio final de gas {quote.tariff}</p>
            <p className="display mt-1 text-[60px] font-semibold leading-none text-paper">
              {fmt.n(kpis.final_price_eur_mwh)}
              <span className="ml-2 text-[17px] font-normal text-ink-2">€/MWh</span>
            </p>
            <p className="mt-2 text-[12.5px] leading-snug text-ink-3">
              {isFixed ? "Precio cerrado para el año" : "Precio esperado del indexado a MIBGAS PVB más el fee"}. El término fijo del peaje va repartido entre el consumo anual.
            </p>
          </div>
          <PriceRuler values={values} total={kpis.final_price_eur_mwh} />
        </div>
      </Panel>

      <KpiStrip>
        <Kpi label="MIBGAS PVB baseload" value={fmt.n(kpis.baseload_pvb_eur_mwh)} unit="€/MWh" hint={`Media diaria Day-Ahead de ${quote.market_year}`} />
        <Kpi
          label="Coste de forma estacional"
          value={fmt.signed(kpis.seasonal_shape_cost_eur_mwh)}
          unit="€/MWh"
          hint={`PVB ponderado ${fmt.n(kpis.weighted_pvb_eur_mwh)}; factor ${fmt.n(kpis.shape_factor)}`}
        />
        <Kpi label="Consumo en invierno gasista" value={fmt.n(kpis.winter_consumption_share_pct, 1)} unit="%" hint="De octubre a marzo, modulado por los HDD" />
        <Kpi label="Desbalance Enagás" value={fmt.n(kpis.imbalance_cost_eur_mwh)} unit="€/MWh" hint="Tarifa de compra frente a la de venta" />
        <Kpi label="Prima de riesgo" value={fmt.n(kpis.risk_premium_eur_mwh)} unit="€/MWh" hint={isFixed ? "Mes más caro frente a la mediana" : "El cliente asume el riesgo de precio"} />
        <Kpi label="Peajes y cargos" value={fmt.n(kpis.regulated_cost_eur_mwh)} unit="€/MWh" hint="Término variable, fijo y cuotas" />
        <Kpi label="Factura anual" value={fmt.eur(kpis.annual_bill_eur)} hint={`${fmt.n(quote.annual_consumption_mwh, 1)} MWh al año`} />
        <Kpi label="Margen bruto esperado" value={fmt.eur(kpis.expected_gross_margin_eur)} hint={`${fmt.n(kpis.margin_eur_mwh)} €/MWh de ${isFixed ? "margen" : "fee"}`} />
      </KpiStrip>

      <div className="grid gap-5 2xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Panel
          title="Consumo mensual frente al precio PVB"
          note="Dos escalas, dos gráficos alineados por mes: cuanto más consumo cae en los meses caros, mayor es el coste de forma."
        >
          <p className="mb-1 flex items-center gap-1.5 text-[12.5px] text-ink-2">
            <span aria-hidden className="inline-block size-2.5 rounded-[2px] bg-s1" />
            Consumo del cliente, MWh al mes
          </p>
          <ResponsiveContainer width="100%" height={170}>
            <BarChart data={rows} syncId="gas-months" margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap="22%">
              <CartesianGrid {...grid} />
              <XAxis dataKey="month" {...axis} tick={false} height={6} />
              <YAxis {...axis} axisLine={false} width={44} tickCount={4} />
              <Tooltip cursor={{ fill: C.raised, opacity: 0.5 }} content={monthTip} />
              <Bar dataKey="consumption" fill={C.s1} radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
          <p className="mb-1 mt-3 flex items-center gap-1.5 text-[12.5px] text-ink-2">
            <svg width="18" height="8" aria-hidden>
              <line x1="0" y1="4" x2="18" y2="4" stroke={C.s2} strokeWidth="2" />
            </svg>
            MIBGAS PVB medio, €/MWh (la línea discontinua es el baseload anual)
          </p>
          <ResponsiveContainer width="100%" height={170}>
            <LineChart data={rows} syncId="gas-months" margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid {...grid} />
              <XAxis dataKey="month" {...axis} padding={{ left: 22, right: 22 }} />
              <YAxis {...axis} axisLine={false} width={44} tickCount={4} domain={["auto", "auto"]} />
              <ReferenceLine y={kpis.baseload_pvb_eur_mwh} stroke={C.neutral} strokeDasharray="4 4" />
              <Tooltip cursor={{ stroke: C.lineStrong }} content={monthTip} />
              <Line dataKey="pvb" stroke={C.s2} strokeWidth={2} dot={{ r: 4, fill: C.s2, stroke: C.panel, strokeWidth: 2 }} activeDot={{ r: 6, stroke: C.panel, strokeWidth: 2 }} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </Panel>

        <Panel title="Desglose auditable" note="Cada línea se redondea al céntimo y el total es su suma exacta.">
          <DataTable
            head={["Componente", "Tratamiento", "€/MWh", "% del precio"]}
            rows={quote.build_up.map((line) => [line.label, line.pass_through ? "Repercutido" : "Cerrado", fmt.signed(line.value_eur_mwh).replace("+", ""), `${fmt.n((100 * line.value_eur_mwh) / kpis.final_price_eur_mwh, 1)} %`])}
            foot={["Precio final", "", fmt.n(kpis.final_price_eur_mwh), "100,0 %"]}
          />
        </Panel>
      </div>
      <p className="text-[12px] leading-snug text-ink-3">{quote.regulatory_reference}</p>
    </>
  );
}

export default function GasPricerTab() {
  const [request, setRequest] = useState<GasQuoteRequest>(INITIAL);
  const state = useApi(quoteGas, request);
  const patch = (changes: Partial<GasQuoteRequest>) => setRequest((current) => ({ ...current, ...changes }));

  return (
    <Workbench
      controls={
        <>
          <ControlGroup title="Suministro">
            <Segmented<GasTariff>
              label="Peaje de red local"
              value={request.tariff}
              onChange={(tariff) => patch({ tariff, annual_consumption_mwh: TARIFF_VOLUMES[tariff].typical })}
              options={(Object.keys(TARIFF_VOLUMES) as GasTariff[]).map((tariff) => ({ value: tariff, label: tariff }))}
            />
            <Select<GasProfileType> label="Perfil de consumo" value={request.profile} onChange={(profile) => patch({ profile })} options={PROFILES} hint="Define qué parte del consumo sigue a la temperatura (HDD base 18 °C)." />
            <NumberField label="Consumo anual" unit="MWh" value={request.annual_consumption_mwh} onChange={(annual_consumption_mwh) => patch({ annual_consumption_mwh })} min={0.5} max={100000} step={request.annual_consumption_mwh < 20 ? 0.5 : 5} hint={`Banda de ${request.tariff}: ${TARIFF_VOLUMES[request.tariff].band}.`} />
          </ControlGroup>
          <ControlGroup title="Producto">
            <Segmented<GasProduct>
              label="Estructura de precio"
              value={request.product}
              onChange={(product) => patch({ product })}
              options={[
                { value: "FIXED", label: "Fijo" },
                { value: "INDEXED", label: "Indexado a PVB" },
              ]}
            />
            <Slider label={request.product === "FIXED" ? "Margen comercial" : "Fee de gestión"} value={request.margin_eur_mwh} onChange={(margin_eur_mwh) => patch({ margin_eur_mwh })} min={0} max={12} step={0.25} display={`${fmt.n(request.margin_eur_mwh)} €/MWh`} />
          </ControlGroup>
          <ControlGroup title="Riesgo">
            <Slider label="Error de nominación diaria" value={request.forecast_error_pct} onChange={(forecast_error_pct) => patch({ forecast_error_pct })} min={0} max={25} step={0.5} display={`${fmt.n(request.forecast_error_pct, 1)} %`} hint="Desviación típica del consumo diario frente a lo nominado en el PVB." />
            <Slider label="Aversión al riesgo" value={request.risk_aversion} onChange={(risk_aversion) => patch({ risk_aversion })} min={0} max={0.5} step={0.01} display={`${fmt.n(request.risk_aversion * 100, 0)} %`} />
          </ControlGroup>
        </>
      }
    >
      <Results state={state}>{(quote) => <QuoteView quote={quote} />}</Results>
    </Workbench>
  );
}
