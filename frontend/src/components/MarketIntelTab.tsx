"use client";

import { Bar, BarChart, CartesianGrid, LabelList, Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";

import { getMarketIntelligence, type ArchetypeShapePoint, type CorrelationPoint, type HourlyMarketPoint, type MarketIntelligenceResponse, type MonthlyMarketPoint } from "@/lib/api";

import { C, ChartUnit, Kpi, KpiStrip, Legend, MONTHS, Panel, Results, Tip, axis, fmt, grid, tipDatum, useApi } from "./ui";

const fetchIntelligence = (_: null, signal: AbortSignal) => getMarketIntelligence(signal);

interface ImbalanceRow extends HourlyMarketPoint {
  longSide: number;
}

interface AncillaryRow extends MonthlyMarketPoint {
  label: string;
}

const ARCHETYPE_LABELS: Record<string, string> = {
  INDUSTRIAL_FLAT: "Industrial plano 24/7",
  COMMERCIAL_DAY: "Comercial, horario de oficina",
  RESIDENTIAL_EVENING: "Residencial, pico nocturno",
  SOLAR_PROSUMER: "Prosumidor con autoconsumo solar",
  SOLAR_ALIGNED_INDUSTRIAL: "Industrial alineado con el sol",
};

/** Shape cost de cada arquetipo en barras divergentes: a la izquierda compra bajo el baseload; a la derecha, sobre él. */
function ArchetypeShapeBars({ shapes }: { shapes: ArchetypeShapePoint[] }) {
  const ordered = [...shapes].sort((a, b) => a.shape_cost_eur_mwh - b.shape_cost_eur_mwh);
  const largest = Math.max(...ordered.map((point) => Math.abs(point.shape_cost_eur_mwh)), 1);
  return (
    <ul className="flex flex-col gap-4">
      {ordered.map((point) => {
        const cost = point.shape_cost_eur_mwh;
        const width = `${(50 * Math.abs(cost)) / largest}%`;
        return (
          <li key={point.profile}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-[13px]">
              <span className="text-ink-2">{ARCHETYPE_LABELS[point.profile] ?? point.profile}</span>
              <span className="font-semibold text-ink">
                {fmt.signed(cost)} €/MWh
                <span className="ml-2 text-[12px] font-normal text-ink-3">
                  compra a {fmt.n(point.captured_price_eur_mwh)} · {fmt.n(point.solar_window_share_pct, 0)} % del consumo entre las 10 y las 17 h
                </span>
              </span>
            </div>
            <div className="relative mt-1.5 h-3 rounded-[3px] bg-desk" aria-hidden>
              <span className="absolute inset-y-0 left-1/2 w-px bg-line-strong" />
              {cost !== 0 && <span className="absolute inset-y-0 rounded-[3px]" style={cost < 0 ? { right: "50%", width, background: C.gain } : { left: "50%", width, background: C.loss }} />}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

const hourTick = (hour: number) => `${hour} h`;
const hourTitle = (hour: number) => `De ${hour}:00 a ${hour + 1}:00`;

function IntelligenceView({ data }: { data: MarketIntelligenceResponse }) {
  const { summary } = data;
  const imbalance: ImbalanceRow[] = data.hourly.map((point) => ({ ...point, longSide: -point.long_discount_mean }));
  const ancillary: AncillaryRow[] = data.monthly.map((point) => ({ ...point, label: MONTHS[point.month - 1] }));
  const winter = data.correlation.filter((point) => point.season === "WINTER");
  const summer = data.correlation.filter((point) => point.season === "SUMMER");

  return (
    <>
      <KpiStrip>
        <Kpi label="OMIE baseload" value={fmt.n(summary.baseload_eur_mwh)} unit="€/MWh" hint={`Año de mercado ${data.market_year}`} />
        <Kpi label="Salto de la curva de pato" value={fmt.n(summary.duck_curve_spread_eur_mwh)} unit="€/MWh" hint={`De ${fmt.n(summary.solar_hours_price_eur_mwh)} en horas solares a ${fmt.n(summary.evening_peak_price_eur_mwh)} por la tarde`} />
        <Kpi label="Horas a precio cero o negativo" value={fmt.n(summary.zero_or_negative_hours, 0)} unit="h" hint={`${fmt.n((100 * summary.zero_or_negative_hours) / 8760, 1)} % del año, por excedente renovable`} />
        <Kpi label="Servicios de ajuste" value={fmt.n(summary.ancillary_mean_eur_mwh)} unit="€/MWh" hint="Media anual sobre la demanda" />
        <Kpi label="Recargo por quedar corto" value={fmt.n(summary.short_premium_mean_eur_mwh)} unit="€/MWh" hint="Precio de desvíos a bajar menos spot" />
        <Kpi label="Descuento por quedar largo" value={fmt.n(summary.long_discount_mean_eur_mwh)} unit="€/MWh" hint="Spot menos precio de desvíos a subir" />
        <Kpi label="MIBGAS PVB medio" value={fmt.n(summary.pvb_mean_eur_mwh)} unit="€/MWh" hint={`Invierno ${fmt.signed(summary.pvb_winter_summer_spread_eur_mwh)} sobre verano`} />
        <Kpi label="Correlación OMIE y PVB" value={fmt.n(summary.power_gas_correlation)} hint={`Invierno ${fmt.n(summary.power_gas_correlation_winter)}; verano ${fmt.n(summary.power_gas_correlation_summer)}`} />
      </KpiStrip>

      <div className="grid gap-5 xl:grid-cols-2">
        <Panel
          title="Curva de pato del mercado diario"
          note="Precio medio OMIE por hora local. La fotovoltaica hunde las horas centrales, sobre todo en primavera, y el precio remonta al caer el sol."
          aside={
            <Legend
              items={[
                { label: "Invierno", color: C.s1, shape: "line" },
                { label: "Primavera", color: C.s2, shape: "line" },
                { label: "Verano", color: C.s3, shape: "line" },
                { label: "Media anual", color: C.neutral, shape: "dash" },
              ]}
            />
          }
        >
          <ChartUnit>€/MWh</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={data.hourly} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid {...grid} />
              <XAxis dataKey="hour" {...axis} interval={2} tickFormatter={hourTick} />
              <YAxis {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ stroke: C.lineStrong }}
                content={(props) => {
                  const row = tipDatum<HourlyMarketPoint>(props);
                  if (!row) return null;
                  return (
                    <Tip
                      title={hourTitle(row.hour)}
                      rows={[
                        { label: "Invierno", value: fmt.n(row.spot_winter), color: C.s1 },
                        { label: "Primavera", value: fmt.n(row.spot_spring), color: C.s2 },
                        { label: "Verano", value: fmt.n(row.spot_summer), color: C.s3 },
                        { label: "Media anual", value: `${fmt.n(row.spot_mean)} €/MWh`, color: C.neutral },
                      ]}
                      footer={`${fmt.n(row.zero_price_share_pct, 1)} % de los días a precio cero o negativo`}
                    />
                  );
                }}
              />
              <Line dataKey="spot_mean" stroke={C.neutral} strokeWidth={2} strokeDasharray="5 4" dot={false} activeDot={false} isAnimationActive={false} />
              <Line dataKey="spot_winter" stroke={C.s1} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: C.panel, strokeWidth: 2 }} isAnimationActive={false} />
              <Line dataKey="spot_spring" stroke={C.s2} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: C.panel, strokeWidth: 2 }} isAnimationActive={false} />
              <Line dataKey="spot_summer" stroke={C.s3} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: C.panel, strokeWidth: 2 }} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </Panel>

        <Panel
          title="Asimetría de los desvíos REE"
          note="Distancia media al spot de cada precio de desvío. Quedar corto penaliza más en la rampa de tarde; quedar largo, en la de mañana."
          aside={
            <Legend
              items={[
                { label: "Corto: recargo sobre spot", color: C.s2 },
                { label: "Largo: descuento bajo spot", color: C.s1 },
              ]}
            />
          }
        >
          <ChartUnit>€/MWh frente al spot</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={imbalance} margin={{ top: 16, right: 8, bottom: 0, left: 0 }} barCategoryGap="18%" stackOffset="sign">
              <CartesianGrid {...grid} />
              <XAxis dataKey="hour" {...axis} interval={2} tickFormatter={hourTick} />
              <YAxis {...axis} axisLine={false} width={44} tickFormatter={(value: number) => fmt.n(Math.abs(value), 0)} />
              <ReferenceLine y={0} stroke={C.lineStrong} />
              <Tooltip
                cursor={{ fill: C.raised, opacity: 0.5 }}
                content={(props) => {
                  const row = tipDatum<ImbalanceRow>(props);
                  if (!row) return null;
                  return (
                    <Tip
                      title={hourTitle(row.hour)}
                      rows={[
                        { label: "Corto, media", value: `+${fmt.n(row.short_premium_mean)} €/MWh`, color: C.s2 },
                        { label: "Corto, P90", value: `+${fmt.n(row.short_premium_p90)} €/MWh` },
                        { label: "Largo, media", value: `−${fmt.n(row.long_discount_mean)} €/MWh`, color: C.s1 },
                        { label: "Largo, P90", value: `−${fmt.n(row.long_discount_p90)} €/MWh` },
                      ]}
                    />
                  );
                }}
              />
              <Bar dataKey="short_premium_mean" stackId="spread" fill={C.s2} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              <Bar dataKey="longSide" stackId="spread" fill={C.s1} radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel
          title="Servicios de ajuste por mes"
          note="Coste medio repercutido a la demanda. El escalón de primavera refleja la operación reforzada de REE desde el 28 de abril de 2025."
          aside={
            <Legend
              items={[
                { label: "Restricciones técnicas", color: C.s1 },
                { label: "Reservas y banda", color: C.s2 },
                { label: "Otros saldos", color: C.s3 },
              ]}
            />
          }
        >
          <ChartUnit>€/MWh</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={ancillary} margin={{ top: 16, right: 8, bottom: 0, left: 0 }} barCategoryGap="26%">
              <CartesianGrid {...grid} />
              <XAxis dataKey="label" {...axis} />
              <YAxis {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ fill: C.raised, opacity: 0.5 }}
                content={(props) => {
                  const row = tipDatum<AncillaryRow>(props);
                  if (!row) return null;
                  return (
                    <Tip
                      title={row.label}
                      rows={[
                        { label: "Restricciones técnicas", value: fmt.n(row.ancillary_constraints_eur_mwh), color: C.s1 },
                        { label: "Reservas y banda", value: fmt.n(row.ancillary_reserves_eur_mwh), color: C.s2 },
                        { label: "Otros saldos", value: fmt.n(row.ancillary_other_eur_mwh), color: C.s3 },
                        { label: "Total", value: `${fmt.n(row.ancillary_total_eur_mwh)} €/MWh` },
                      ]}
                      footer={`${fmt.n(row.zero_or_negative_hours, 0)} horas a precio cero o negativo`}
                    />
                  );
                }}
              />
              <Bar dataKey="ancillary_constraints_eur_mwh" stackId="ssaa" fill={C.s1} stroke={C.panel} strokeWidth={2} isAnimationActive={false} />
              <Bar dataKey="ancillary_reserves_eur_mwh" stackId="ssaa" fill={C.s2} stroke={C.panel} strokeWidth={2} isAnimationActive={false} />
              <Bar dataKey="ancillary_other_eur_mwh" stackId="ssaa" fill={C.s3} stroke={C.panel} strokeWidth={2} radius={[4, 4, 0, 0]} isAnimationActive={false}>
                <LabelList dataKey="ancillary_total_eur_mwh" position="top" fill={C.ink2} fontSize={12} formatter={(value: unknown) => fmt.n(Number(value), 1)} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel
          title="Gas y electricidad, día a día"
          note={`Cada punto es un día: PVB Day-Ahead frente al baseload OMIE. Correlación de Pearson ${fmt.n(summary.power_gas_correlation)}: parcial, porque el ciclo combinado solo marca precio cuando la demanda residual es alta.`}
          aside={
            <Legend
              items={[
                { label: "Invierno gasista (oct-mar)", color: C.s1 },
                { label: "Verano gasista (abr-sep)", color: C.s2 },
              ]}
            />
          }
        >
          <ChartUnit>OMIE baseload diario, €/MWh</ChartUnit>
          <ResponsiveContainer width="100%" height={300}>
            <ScatterChart margin={{ top: 16, right: 12, bottom: 18, left: 0 }}>
              <CartesianGrid stroke={C.line} />
              <XAxis type="number" dataKey="pvb_eur_mwh" {...axis} domain={["dataMin - 2", "dataMax + 2"]} tickFormatter={(value: number) => fmt.n(value, 0)} label={{ value: "MIBGAS PVB, €/MWh", position: "insideBottom", offset: -12, fill: C.ink3, fontSize: 12 }} />
              <YAxis type="number" dataKey="baseload_eur_mwh" {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ stroke: C.lineStrong }}
                content={(props) => {
                  const point = tipDatum<CorrelationPoint>(props);
                  if (!point) return null;
                  const [year, month, day] = point.day.split("-");
                  return (
                    <Tip
                      title={`${Number(day)} ${MONTHS[Number(month) - 1].toLowerCase()} ${year}`}
                      rows={[
                        { label: "MIBGAS PVB", value: `${fmt.n(point.pvb_eur_mwh)} €/MWh` },
                        { label: "OMIE baseload", value: `${fmt.n(point.baseload_eur_mwh)} €/MWh`, color: point.season === "WINTER" ? C.s1 : C.s2 },
                      ]}
                    />
                  );
                }}
              />
              <Scatter data={winter} fill={C.s1} fillOpacity={0.85} stroke={C.panel} strokeWidth={1} isAnimationActive={false} />
              <Scatter data={summer} fill={C.s2} fillOpacity={0.85} stroke={C.panel} strokeWidth={1} isAnimationActive={false} />
            </ScatterChart>
          </ResponsiveContainer>
        </Panel>
      </div>

      <Panel
        title="Quién paga la curva de pato: shape cost por arquetipo"
        note={`Precio OMIE ponderado por la curva de cada cliente menos el baseload de ${fmt.n(summary.baseload_eur_mwh)} €/MWh. No decide cuánta energía solar usa el cliente, sino a qué horas compra de la red.`}
        aside={
          <Legend
            items={[
              { label: "Compra bajo el baseload", color: C.gain },
              { label: "Compra sobre el baseload", color: C.loss },
            ]}
          />
        }
      >
        <div className="grid gap-x-10 gap-y-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,38ch)]">
          <ArchetypeShapeBars shapes={data.archetype_shapes} />
          <div className="flex flex-col gap-3 text-[13px] leading-snug text-ink-2">
            <p>
              <span className="font-semibold text-ink">El prosumidor es el cliente más caro de perfilar.</span> Sus placas cubren el mediodía, que es justo la energía barata, y de la red solo compra la mañana y la punta de las 21 h. Compra menos MWh, pero cada uno
              cuesta más que el de un hogar sin placas.
            </p>
            <p>
              <span className="font-semibold text-ink">La carga que sigue al sol cobra por su forma.</span> Un bombeo solar, una desaladora o un frío industrial flexible concentran el consumo de 10 a 17 h y capturan las horas de precio valle o cero.
            </p>
            <p className="text-ink-3">Cotizar al prosumidor con descuento «por ser solar» destruye margen; el descuento corresponde a quien desplaza consumo hacia el mediodía.</p>
          </div>
        </div>
      </Panel>
    </>
  );
}

export default function MarketIntelTab() {
  const state = useApi(fetchIntelligence, null, 0);
  return (
    <div className="flex flex-col gap-5">
      <Results state={state}>{(data) => <IntelligenceView data={data} />}</Results>
    </div>
  );
}
