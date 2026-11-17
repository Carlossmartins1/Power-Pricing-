"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { runBacktest, type BacktestRequest, type BacktestResponse, type ContractResult, type StressScenario } from "@/lib/api";

import { C, ChartUnit, ControlGroup, DataTable, Kpi, KpiStrip, Legend, Notice, Panel, Results, Slider, Tip, Workbench, axis, fmt, grid, tipDatum, useApi } from "./ui";

const BASE_STRESS: StressScenario = { volume_shock_pct: 0, shape_shift_pct: 0, imbalance_multiplier: 1, unhedged_ratio: 0.15, wholesale_price_shock_pct: 0 };

const PRESETS: { label: string; hint: string; stress: StressScenario }[] = [
  { label: "Base", hint: "Sin estrés, 15 % abierto", stress: BASE_STRESS },
  { label: "Ola de calor", hint: "Más demanda y desvíos caros", stress: { volume_shock_pct: 15, shape_shift_pct: 5, imbalance_multiplier: 2, unhedged_ratio: 0.15, wholesale_price_shock_pct: 12 } },
  { label: "Tensión de precios", hint: "Spot al alza con cartera abierta", stress: { volume_shock_pct: 5, shape_shift_pct: 0, imbalance_multiplier: 2.5, unhedged_ratio: 0.4, wholesale_price_shock_pct: 30 } },
  { label: "Más autoconsumo", hint: "La demanda sale de las horas solares", stress: { volume_shock_pct: -8, shape_shift_pct: 25, imbalance_multiplier: 1.3, unhedged_ratio: 0.15, wholesale_price_shock_pct: -5 } },
];

const PROFILE_LABELS: Record<string, string> = {
  INDUSTRIAL_FLAT: "Industrial plano",
  COMMERCIAL_DAY: "Comercial diurno",
  RESIDENTIAL_EVENING: "Residencial nocturno",
  SOLAR_PROSUMER: "Prosumidor solar",
  SOLAR_ALIGNED_INDUSTRIAL: "Industrial solar",
  RESIDENTIAL_HEATING: "Calefacción residencial",
  COMMERCIAL: "Comercial",
  INDUSTRIAL_PROCESS: "Proceso industrial",
};

const EFFECT_LABELS: [keyof ContractResult, string][] = [
  ["volume_effect_eur", "Volumen"],
  ["shape_effect_eur", "Perfil"],
  ["wholesale_hedging_effect_eur", "Mayorista"],
  ["imbalance_effect_eur", "Desvíos"],
];

function worstEffect(contract: ContractResult): string {
  const [key, label] = EFFECT_LABELS.reduce((worst, current) => ((contract[current[0]] as number) < (contract[worst[0]] as number) ? current : worst));
  return `${label} ${fmt.eurSigned(contract[key] as number)}`;
}

/** Los cuatro efectos a su propia escala: en el puente quedan pequeños frente al margen total. */
function EffectBars({ steps, reference }: { steps: StepRow[]; reference: number }) {
  const effects = steps.filter((step) => step.kind === "effect");
  const largest = Math.max(...effects.map((step) => Math.abs(step.value)), 1);
  return (
    <ul className="flex flex-col gap-4">
      {effects.map((step) => {
        const width = `${(50 * Math.abs(step.value)) / largest}%`;
        return (
          <li key={step.label}>
            <div className="flex items-baseline justify-between gap-3 text-[13px]">
              <span className="text-ink-2">{step.label}</span>
              <span className="font-semibold text-ink">
                {fmt.eurSigned(step.value)}
                <span className="ml-2 text-[12px] font-normal text-ink-3">{reference ? `${fmt.signed((100 * step.value) / reference, 1)} %` : ""}</span>
              </span>
            </div>
            <div className="relative mt-1.5 h-3 rounded-[3px] bg-desk" aria-hidden>
              <span className="absolute inset-y-0 left-1/2 w-px bg-line-strong" />
              <span className="absolute inset-y-0 rounded-[3px]" style={step.value >= 0 ? { left: "50%", width, background: C.gain } : { right: "50%", width, background: C.loss }} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

const STEP_SHORT_LABELS: Record<string, string> = {
  ex_ante: "Ex-ante",
  volume_effect_eur: "Volumen",
  shape_effect_eur: "Perfil",
  wholesale_hedging_effect_eur: "Mayorista",
  imbalance_effect_eur: "Desvíos",
  ex_post: "Ex-post",
};

interface StepRow {
  label: string;
  short: string;
  range: [number, number];
  value: number;
  end: number;
  kind: "total" | "effect";
  color: string;
  text: string;
}

function BacktestView({ result }: { result: BacktestResponse }) {
  const { kpis } = result;
  const steps: StepRow[] = result.waterfall.map((step) => ({
    label: step.label,
    short: STEP_SHORT_LABELS[step.key] ?? step.label,
    range: step.kind === "total" ? [Math.min(0, step.value_eur), Math.max(0, step.value_eur)] : [Math.min(step.start_eur, step.end_eur), Math.max(step.start_eur, step.end_eur)],
    value: step.value_eur,
    end: step.end_eur,
    kind: step.kind as "total" | "effect",
    color: step.kind === "total" ? C.neutral : step.value_eur >= 0 ? C.gain : C.loss,
    text: step.kind === "total" ? fmt.kEur(step.value_eur) : `${step.value_eur > 0 ? "+" : step.value_eur < 0 ? "−" : ""}${fmt.kEur(Math.abs(step.value_eur))}`,
  }));
  const delta = kpis.margin_delta_eur;

  return (
    <>
      <KpiStrip>
        <Kpi label="Margen bruto ex-ante" value={fmt.eur(kpis.ex_ante_margin_eur)} hint={`${fmt.n(kpis.ex_ante_unit_margin_eur_mwh)} €/MWh sobre ${fmt.n(kpis.contracted_mwh, 0)} MWh contratados`} />
        <Kpi label="Margen bruto ex-post" value={fmt.eur(kpis.ex_post_margin_eur)} hint={`${fmt.n(kpis.ex_post_unit_margin_eur_mwh)} €/MWh sobre ${fmt.n(kpis.realized_mwh, 0)} MWh reales`} />
        <Kpi label="Variación de margen" value={fmt.eurSigned(delta)} tone={delta < 0 ? "loss" : "gain"} hint={`${fmt.signed(kpis.margin_delta_pct, 1)} % frente a lo esperado al firmar`} />
        <Kpi
          label="Contratos a revisar"
          value={fmt.n(kpis.contracts_at_risk, 0)}
          unit={`de ${fmt.n(kpis.contracts, 0)}`}
          tone={kpis.contracts_at_risk > 0 ? "loss" : undefined}
          hint={`${fmt.n(kpis.at_risk_volume_share_pct, 1)} % del volumen bajo ${fmt.n(result.margin_threshold_eur_mwh)} €/MWh`}
        />
      </KpiStrip>

      <div className="grid gap-5 2xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <Panel
        title="Atribución del P&L: del margen esperado al realizado"
        note={`Los cuatro efectos reconcilian la diferencia al céntimo (residuo de redondeo: ${fmt.n(Math.abs(result.reconciliation_error_eur))} €). Posición abierta: ${fmt.n(kpis.open_position_mwh, 0)} MWh.`}
        aside={
          <Legend
            items={[
              { label: "Margen total", color: C.neutral },
              { label: "Efecto a favor", color: C.gain },
              { label: "Efecto en contra", color: C.loss },
            ]}
          />
        }
      >
        <ChartUnit>Miles de €</ChartUnit>
          <ResponsiveContainer width="100%" height={340}>
          <BarChart data={steps} margin={{ top: 20, right: 8, bottom: 0, left: 0 }} barCategoryGap="16%">
            <CartesianGrid {...grid} />
            <XAxis dataKey="short" {...axis} interval={0} />
            <YAxis {...axis} axisLine={false} width={56} tickFormatter={(value: number) => fmt.n(value / 1000, 0)} />
            <ReferenceLine y={0} stroke={C.lineStrong} />
            <Tooltip
              cursor={{ fill: C.raised, opacity: 0.5 }}
              content={(props) => {
                const row = tipDatum<StepRow>(props);
                if (!row) return null;
                return (
                  <Tip
                    title={row.label}
                    rows={row.kind === "total" ? [{ label: "Margen bruto", value: fmt.eur(row.value), color: row.color }] : [{ label: "Efecto", value: fmt.eurSigned(row.value), color: row.color }, { label: "Margen acumulado", value: fmt.eur(row.end) }]}
                    footer={row.kind === "effect" && kpis.ex_ante_margin_eur ? `${fmt.signed((100 * row.value) / kpis.ex_ante_margin_eur, 1)} % del margen ex-ante` : undefined}
                  />
                );
              }}
            />
            <Bar dataKey="range" radius={4} isAnimationActive={false}>
              {steps.map((step) => (
                <Cell key={step.label} fill={step.color} />
              ))}
              <LabelList dataKey="text" position="top" fill={C.ink} fontSize={12.5} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </Panel>
      <Panel title="Los cuatro efectos, a su escala" note="Importe de cada efecto y su peso sobre el margen ex-ante. A la derecha del eje suma; a la izquierda resta.">
        <EffectBars steps={steps} reference={kpis.ex_ante_margin_eur} />
      </Panel>
      </div>

      <Panel title="Margen por segmento" note="Electricidad por tarifa de acceso y gas por peaje de red local.">
        <DataTable
          head={["Segmento", "Contratos", "MWh contratados", "MWh reales", "Ex-ante €", "Ex-post €", "Variación", "€/MWh real", "A revisar"]}
          rows={result.segments.map((segment) => {
            const change = segment.ex_post_margin_eur - segment.ex_ante_margin_eur;
            return [
              `${segment.commodity === "POWER" ? "Electricidad" : "Gas"} ${segment.tariff}`,
              fmt.n(segment.contracts, 0),
              fmt.n(segment.contracted_mwh, 0),
              fmt.n(segment.realized_mwh, 0),
              fmt.n(segment.ex_ante_margin_eur, 0),
              fmt.n(segment.ex_post_margin_eur, 0),
              <span key="change" className={change < 0 ? "text-loss" : "text-gain"}>{fmt.eurSigned(change)}</span>,
              fmt.n(segment.realized_unit_margin_eur_mwh),
              fmt.n(segment.contracts_at_risk, 0),
            ];
          })}
          foot={["Cartera", fmt.n(kpis.contracts, 0), fmt.n(kpis.contracted_mwh, 0), fmt.n(kpis.realized_mwh, 0), fmt.n(kpis.ex_ante_margin_eur, 0), fmt.n(kpis.ex_post_margin_eur, 0), fmt.eurSigned(delta), fmt.n(kpis.ex_post_unit_margin_eur_mwh), fmt.n(kpis.contracts_at_risk, 0)]}
        />
      </Panel>

      <Panel
        title="Alertas de repricing"
        note={`Contratos con margen realizado por debajo de ${fmt.n(result.margin_threshold_eur_mwh)} €/MWh, del peor al mejor. Se muestran ${result.at_risk_contracts.length} de ${kpis.contracts_at_risk}.`}
      >
        {result.at_risk_contracts.length === 0 ? (
          <Notice tone="info">Ningún contrato cae por debajo del umbral en este escenario. Sube el umbral o endurece el estrés para ver candidatos a repricing.</Notice>
        ) : (
          <DataTable
            maxHeight={460}
            head={["Contrato", "Tarifa", "Perfil", "Producto", "Firmado hace", "MWh reales", "Precio €/MWh", "Margen pactado", "Margen real", "Efecto que más resta", "Alerta"]}
            rows={result.at_risk_contracts.map((contract) => [
              contract.contract_id,
              contract.tariff,
              PROFILE_LABELS[contract.profile] ?? contract.profile,
              contract.product === "FIXED" ? "Fijo" : "Indexado",
              `${contract.signed_months_ago} meses`,
              fmt.n(contract.realized_mwh, contract.realized_mwh < 100 ? 1 : 0),
              fmt.n(contract.contract_price_eur_mwh),
              fmt.n(contract.ex_ante_unit_margin_eur_mwh),
              <span key="margin" className={`font-semibold ${contract.realized_unit_margin_eur_mwh < 0 ? "text-loss" : "text-ink"}`}>{fmt.signed(contract.realized_unit_margin_eur_mwh).replace("+", "")}</span>,
              worstEffect(contract),
              <span key="alert" className="rounded border border-loss/60 px-1.5 py-0.5 text-[11.5px] font-semibold text-loss">{contract.alert}</span>,
            ])}
          />
        )}
      </Panel>

      <Panel title="Supuestos del back-test">
        <ul className="list-disc space-y-1 pl-5 text-[12.5px] leading-snug text-ink-2">
          {result.assumptions.map((assumption) => (
            <li key={assumption}>{assumption}</li>
          ))}
        </ul>
      </Panel>
    </>
  );
}

export default function PortfolioBacktestTab() {
  const [stress, setStress] = useState<StressScenario>(BASE_STRESS);
  const [threshold, setThreshold] = useState(2);
  const request: BacktestRequest = { n_contracts: 500, seed: 7, stress, margin_threshold_eur_mwh: threshold, max_alerts: 25 };
  const state = useApi(runBacktest, request);
  const patch = (changes: Partial<StressScenario>) => setStress((current) => ({ ...current, ...changes }));
  const activePreset = PRESETS.find((preset) => JSON.stringify(preset.stress) === JSON.stringify(stress))?.label;

  return (
    <Workbench
      controls={
        <>
          <ControlGroup title="Escenarios">
            <div className="grid grid-cols-2 gap-2">
              {PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  aria-pressed={activePreset === preset.label}
                  onClick={() => setStress(preset.stress)}
                  className={`rounded-md border px-2.5 py-2 text-left transition-colors ${activePreset === preset.label ? "border-paper bg-raised" : "border-line hover:border-line-strong hover:bg-raised"}`}
                >
                  <span className="block text-[13px] font-semibold text-ink">{preset.label}</span>
                  <span className="mt-0.5 block text-[11.5px] leading-snug text-ink-3">{preset.hint}</span>
                </button>
              ))}
            </div>
          </ControlGroup>
          <ControlGroup title="Estrés climático">
            <Slider label="Desviación de volumen" value={stress.volume_shock_pct} onChange={(volume_shock_pct) => patch({ volume_shock_pct })} min={-30} max={30} step={1} display={`${fmt.signed(stress.volume_shock_pct, 0)} %`} hint="Ola de calor o de frío: demanda real frente a la contratada." />
            <Slider label="Desplazamiento solar del perfil" value={stress.shape_shift_pct} onChange={(shape_shift_pct) => patch({ shape_shift_pct })} min={-30} max={30} step={1} display={`${fmt.signed(stress.shape_shift_pct, 0)} %`} hint="Positivo: el consumo sale de las horas solares hacia las caras." />
          </ControlGroup>
          <ControlGroup title="Estrés de mercado">
            <Slider label="Spot OMIE / PVB realizado" value={stress.wholesale_price_shock_pct} onChange={(wholesale_price_shock_pct) => patch({ wholesale_price_shock_pct })} min={-30} max={50} step={1} display={`${fmt.signed(stress.wholesale_price_shock_pct, 0)} %`} />
            <Slider label="Posición sin cubrir" value={stress.unhedged_ratio} onChange={(unhedged_ratio) => patch({ unhedged_ratio })} min={0} max={1} step={0.05} display={`${fmt.n(stress.unhedged_ratio * 100, 0)} %`} hint="Parte del volumen contratado que se compra a spot." />
            <Slider label="Spreads de desvío REE / Enagás" value={stress.imbalance_multiplier} onChange={(imbalance_multiplier) => patch({ imbalance_multiplier })} min={0.5} max={4} step={0.1} display={`× ${fmt.n(stress.imbalance_multiplier, 1)}`} />
          </ControlGroup>
          <ControlGroup title="Alerta de repricing">
            <Slider label="Margen mínimo aceptable" value={threshold} onChange={setThreshold} min={0} max={6} step={0.25} display={`${fmt.n(threshold)} €/MWh`} />
          </ControlGroup>
        </>
      }
    >
      <Results state={state}>{(result) => <BacktestView result={result} />}</Results>
    </Workbench>
  );
}
