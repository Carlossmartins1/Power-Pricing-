"use client";

import { useEffect, useState, type ReactNode } from "react";

/** Colores en hexadecimal para Recharts (los mismos tokens que `globals.css`). */
export const C = {
  desk: "#131c28",
  panel: "#1a2533",
  raised: "#223041",
  line: "#2e3d50",
  lineStrong: "#40536b",
  ink: "#e8edf2",
  ink2: "#aab6c3",
  ink3: "#8593a3",
  paper: "#f3ead9",
  s1: "#3987e5",
  s2: "#d95926",
  s3: "#199e70",
  s4: "#c98500",
  s5: "#d55181",
  s6: "#0ca30c",
  gain: "#3987e5",
  loss: "#e66767",
  neutral: "#7f8ea0",
} as const;

const number = (digits: number) => new Intl.NumberFormat("es-ES", { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: "always" });
const formatters = [number(0), number(1), number(2)];

export const fmt = {
  n: (value: number, digits: 0 | 1 | 2 = 2) => formatters[digits].format(value),
  signed: (value: number, digits: 0 | 1 | 2 = 2) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${formatters[digits].format(Math.abs(value))}`,
  eur: (value: number) => `${formatters[0].format(Math.round(value))} €`,
  eurSigned: (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${formatters[0].format(Math.abs(Math.round(value)))} €`,
  kEur: (value: number) => `${formatters[Math.abs(value) >= 100_000 ? 0 : 1].format(value / 1000)} k€`,
};

export const MONTHS = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"];

// ---------------------------------------------------------------------------------------------
// Datos: petición con debounce y cancelación
// ---------------------------------------------------------------------------------------------

export interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * Lanza `call(request)` cada vez que cambia la petición, esperando `delay` ms para agrupar
 * movimientos de slider. Conserva el último resultado válido mientras llega el siguiente.
 */
export function useApi<TReq, TRes>(call: (request: TReq, signal: AbortSignal) => Promise<TRes>, request: TReq, delay = 220): ApiState<TRes> {
  const [state, setState] = useState<ApiState<TRes>>({ data: null, error: null, loading: true });
  const key = JSON.stringify(request);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setState((previous) => ({ ...previous, loading: true }));
      call(JSON.parse(key) as TReq, controller.signal)
        .then((data) => setState({ data, error: null, loading: false }))
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          const message = cause instanceof Error ? cause.message : "Error desconocido";
          setState((previous) => ({ data: previous.data, error: message, loading: false }));
        });
    }, delay);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [call, key, delay]);
  return state;
}

// ---------------------------------------------------------------------------------------------
// Estructura
// ---------------------------------------------------------------------------------------------

export function Workbench({ controls, children }: { controls: ReactNode; children: ReactNode }) {
  return (
    <div className="grid items-start gap-5 lg:grid-cols-[312px_minmax(0,1fr)]">
      <aside className="rounded-lg border border-line bg-panel lg:sticky lg:top-4">{controls}</aside>
      <div className="flex min-w-0 flex-col gap-5">{children}</div>
    </div>
  );
}

export function ControlGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="border-b border-line px-5 py-4 last:border-b-0">
      <legend className="float-left mb-3 w-full text-[13px] font-semibold text-ink">{title}</legend>
      <div className="clear-both flex flex-col gap-4">{children}</div>
    </fieldset>
  );
}

export function Panel({ title, note, aside, children, className = "" }: { title?: string; note?: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`min-w-0 rounded-lg border border-line bg-panel ${className}`}>
      {title && (
        <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-5 pt-4">
          <div className="max-w-[62ch]">
            <h3 className="text-[15px] font-semibold text-ink">{title}</h3>
            {note && <p className="mt-0.5 text-[12.5px] leading-snug text-ink-3">{note}</p>}
          </div>
          {aside}
        </header>
      )}
      <div className="px-5 pb-5 pt-4">{children}</div>
    </section>
  );
}

export function KpiStrip({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line md:grid-cols-4">{children}</div>;
}

export function Kpi({ label, value, unit, hint, tone }: { label: string; value: string; unit?: string; hint?: string; tone?: "gain" | "loss" }) {
  const color = tone === "loss" ? "text-loss" : tone === "gain" ? "text-gain" : "text-ink";
  return (
    <div className="bg-panel px-4 py-3.5">
      <p className="text-[12.5px] text-ink-3">{label}</p>
      <p className={`display mt-1.5 text-[22px] font-semibold leading-none ${color}`}>
        {value}
        {unit && <span className="ml-1.5 text-[12.5px] font-normal text-ink-3">{unit}</span>}
      </p>
      {hint && <p className="mt-2 text-[12px] leading-snug text-ink-2">{hint}</p>}
    </div>
  );
}

export function Notice({ tone, children }: { tone: "error" | "warning" | "info"; children: ReactNode }) {
  const style = { error: "border-loss/60 text-loss", warning: "border-warn/50 text-warn", info: "border-line-strong text-ink-2" }[tone];
  return (
    <p role={tone === "error" ? "alert" : "status"} className={`rounded-md border bg-panel px-4 py-2.5 text-[13px] leading-snug ${style}`}>
      {children}
    </p>
  );
}

export function Results<T>({ state, children }: { state: ApiState<T>; children: (data: T) => ReactNode }) {
  if (!state.data) {
    return state.error ? <Notice tone="error">{state.error}</Notice> : <Notice tone="info">Calculando con los datos del warehouse…</Notice>;
  }
  return (
    <>
      {state.error && <Notice tone="error">{state.error} Se muestra el último resultado válido.</Notice>}
      <div className={`flex min-w-0 flex-col gap-5 transition-opacity duration-150 ${state.loading ? "opacity-60" : ""}`} aria-busy={state.loading}>
        {children(state.data)}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// Controles
// ---------------------------------------------------------------------------------------------

export function Field({ label, value, hint, children }: { label: string; value?: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-baseline justify-between gap-3 text-[12.5px]">
        <span className="text-ink-2">{label}</span>
        {value && <span className="font-semibold text-ink">{value}</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-[12px] leading-snug text-ink-3">{hint}</span>}
    </label>
  );
}

export function Slider({ label, value, onChange, min, max, step, display, hint }: { label: string; value: number; onChange: (value: number) => void; min: number; max: number; step: number; display: string; hint?: string }) {
  return (
    <Field label={label} value={display} hint={hint}>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </Field>
  );
}

export function Select<T extends string>({ label, value, onChange, options, hint }: { label: string; value: T; onChange: (value: T) => void; options: { value: T; label: string }[]; hint?: string }) {
  return (
    <Field label={label} hint={hint}>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </Field>
  );
}

export function NumberField({ label, value, onChange, min, max, step, unit, hint }: { label: string; value: number; onChange: (value: number) => void; min: number; max: number; step: number; unit: string; hint?: string }) {
  return (
    <Field label={label} value={unit} hint={hint}>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next) && next >= min && next <= max) onChange(next);
        }}
      />
    </Field>
  );
}

export function Segmented<T extends string>({ label, value, onChange, options }: { label: string; value: T; onChange: (value: T) => void; options: { value: T; label: string }[] }) {
  return (
    <div>
      <p className="mb-1.5 text-[12.5px] text-ink-2">{label}</p>
      <div role="radiogroup" aria-label={label} className="grid gap-1 rounded-md border border-line bg-desk p-1" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
        {options.map((option) => {
          const active = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onChange(option.value)}
              className={`rounded px-2 py-1.5 text-[13px] transition-colors ${active ? "bg-paper font-semibold text-desk" : "text-ink-2 hover:bg-raised hover:text-ink"}`}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Piezas de gráfico
// ---------------------------------------------------------------------------------------------

export const axis = {
  tick: { fill: C.ink3, fontSize: 12 },
  axisLine: { stroke: C.lineStrong },
  tickLine: false,
} as const;

export const grid = { stroke: C.line, vertical: false } as const;

/** Unidad del eje vertical, escrita sobre el gráfico para que nunca quede recortada. */
export function ChartUnit({ children }: { children: ReactNode }) {
  return <p className="mb-1 text-[12px] text-ink-3">{children}</p>;
}

export interface LegendItem {
  label: string;
  color: string;
  shape?: "square" | "line" | "dash";
}

export function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-ink-2">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          {item.shape === "line" || item.shape === "dash" ? (
            <svg width="18" height="8" aria-hidden>
              <line x1="0" y1="4" x2="18" y2="4" stroke={item.color} strokeWidth="2" strokeDasharray={item.shape === "dash" ? "4 3" : undefined} />
            </svg>
          ) : (
            <span aria-hidden className="inline-block size-2.5 rounded-[2px]" style={{ background: item.color }} />
          )}
          {item.label}
        </li>
      ))}
    </ul>
  );
}

export interface TipRow {
  label: string;
  value: string;
  color?: string;
}

/** Tooltip común: el valor va en tinta de texto y la marca de color solo identifica la serie. */
export function Tip({ title, rows, footer }: { title: string; rows: TipRow[]; footer?: string }) {
  return (
    <div className="min-w-[170px] rounded-md border border-line-strong bg-raised px-3 py-2 text-[12.5px] shadow-[0_8px_24px_rgba(6,11,18,0.55)]">
      <p className="mb-1.5 font-semibold text-ink">{title}</p>
      <ul className="flex flex-col gap-1">
        {rows.map((row) => (
          <li key={row.label} className="flex items-center justify-between gap-5">
            <span className="flex items-center gap-1.5 text-ink-2">
              {row.color && <span aria-hidden className="inline-block size-2 rounded-[2px]" style={{ background: row.color }} />}
              {row.label}
            </span>
            <span className="font-semibold text-ink">{row.value}</span>
          </li>
        ))}
      </ul>
      {footer && <p className="mt-1.5 border-t border-line pt-1.5 text-[12px] text-ink-3">{footer}</p>}
    </div>
  );
}

/** Props mínimas que Recharts entrega al contenido de un tooltip. */
export interface TipProps<T> {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: T }>;
}

export function tipDatum<T>(props: unknown): T | null {
  const { active, payload } = props as TipProps<T>;
  return active && payload && payload.length > 0 && payload[0].payload ? payload[0].payload : null;
}

/** Tabla densa de cifras con cabecera fija y números alineados a la derecha. */
export function DataTable({ head, rows, foot, maxHeight }: { head: string[]; rows: ReactNode[][]; foot?: ReactNode[]; maxHeight?: number }) {
  return (
    <div className="overflow-auto" style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line-strong text-left text-[12.5px] text-ink-3">
            {head.map((cell, index) => (
              <th key={cell} scope="col" className={`sticky top-0 whitespace-nowrap bg-panel py-2 font-normal ${index === 0 ? "pr-3" : "px-3 text-right last:pr-0"}`}>
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-b border-line last:border-b-0 hover:bg-raised/60">
              {row.map((cell, index) => (
                <td key={index} className={`whitespace-nowrap py-2 ${index === 0 ? "pr-3 text-ink" : "px-3 text-right text-ink-2 last:pr-0"}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {foot && (
          <tfoot>
            <tr className="border-t border-line-strong font-semibold text-ink">
              {foot.map((cell, index) => (
                <td key={index} className={`whitespace-nowrap py-2.5 ${index === 0 ? "pr-3" : "px-3 text-right last:pr-0"}`}>
                  {cell}
                </td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}
