"use client";

import { Download, FileSpreadsheet, Upload, X } from "lucide-react";
import { useId, useRef, useState, type DragEvent } from "react";

import { CurveError, MAX_CSV_BYTES, downloadSampleCurve, hoursInYear, loadFactor, parseLoadCurveCsv, type CurveUnit, type LoadCurve } from "@/lib/loadCurve";

import { C, Notice, Segmented, fmt } from "./ui";

/** Día medio de la curva cargada: 24 barras para comprobar de un vistazo que el fichero se ha leído bien. */
function AverageDay({ curve }: { curve: LoadCurve }) {
  const max = Math.max(...curve.hourlyMean);
  const width = 264;
  const height = 44;
  const slot = width / 24;
  return (
    <svg viewBox={`0 0 ${width} ${height + 12}`} className="mt-3 block w-full" role="img" aria-label="Consumo medio de la curva cargada por hora del día">
      {curve.hourlyMean.map((value, hour) => {
        const bar = max > 0 ? Math.max(1.5, (value / max) * height) : 1.5;
        return (
          <rect key={hour} x={hour * slot + 1} y={height - bar} width={slot - 2} height={bar} rx={1.5} fill={C.s1}>
            <title>{`De ${hour}:00 a ${hour + 1}:00, ${fmt.n(max > 0 ? (100 * value) / max : 0, 0)} % de la hora máxima`}</title>
          </rect>
        );
      })}
      {[0, 6, 12, 18].map((hour) => (
        <text key={hour} x={hour * slot + 1} y={height + 11} fontSize={9.5} fill={C.ink3}>
          {hour} h
        </text>
      ))}
    </svg>
  );
}

interface CurveUploadProps {
  curve: LoadCurve | null;
  unit: CurveUnit;
  /** Año del mercado cargado en la API; `null` si todavía no se conoce. */
  year: number | null;
  onLoad: (curve: LoadCurve) => void;
  onUnitChange: (unit: CurveUnit) => void;
  onClear: () => void;
}

/** Zona para subir la curva horaria del cliente (CSV de 8.760 valores) y descargar un fichero de ejemplo. */
export default function CurveUpload({ curve, unit, year, onLoad, onUnitChange, onClear }: CurveUploadProps) {
  const inputId = useId();
  const hintId = useId();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expectedHours = year === null ? 8760 : hoursInYear(year);

  async function read(file: File | undefined) {
    if (!file) return;
    setError(null);
    if (file.size > MAX_CSV_BYTES) {
      setError(`«${file.name}» ocupa ${fmt.n(file.size / 1_048_576, 1)} MB y el máximo son 2 MB. Una curva horaria de un año ocupa unos 200 kB: exporta solo la columna de consumo.`);
      return;
    }
    setReading(true);
    try {
      onLoad(parseLoadCurveCsv(await file.text(), file.name, year));
    } catch (cause) {
      setError(cause instanceof CurveError ? cause.message : `No se ha podido leer «${file.name}». Comprueba que es un fichero de texto CSV.`);
    } finally {
      setReading(false);
      if (input.current) input.current.value = ""; // permite volver a elegir el mismo fichero tras corregirlo
    }
  }

  function drop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    void read(event.dataTransfer.files[0]);
  }

  const picker = (
    <input
      ref={input}
      id={inputId}
      type="file"
      accept=".csv,.txt,text/csv,text/plain"
      className="sr-only"
      aria-describedby={hintId}
      onChange={(event) => void read(event.target.files?.[0])}
    />
  );

  return (
    <div>
      <p className="mb-1.5 text-[12.5px] text-ink-2">Curva horaria propia</p>

      {curve ? (
        <div className="rounded-md border border-line-strong bg-desk p-3">
          <div className="flex items-start gap-2.5">
            <FileSpreadsheet aria-hidden size={18} className="mt-0.5 shrink-0 text-ink-2" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-semibold text-ink" title={curve.fileName}>
                {curve.fileName}
              </p>
              <p className="mt-0.5 text-[12px] leading-snug text-ink-3">
                {fmt.n(curve.values.length, 0)} horas · {fmt.n(curve.total, curve.total < 1000 ? 1 : 0)} {unit} · punta {fmt.n(curve.peak, curve.peak < 100 ? 1 : 0)} {unit === "kWh" ? "kW" : "MW"} · factor de carga{" "}
                {fmt.n(100 * loadFactor(curve), 0)} %
              </p>
            </div>
            <button type="button" onClick={onClear} aria-label="Quitar la curva y volver al arquetipo" title="Quitar la curva" className="-m-1 rounded p-1 text-ink-3 hover:bg-raised hover:text-ink">
              <X aria-hidden size={16} />
            </button>
          </div>
          <AverageDay curve={curve} />
          <div className="mt-3">
            <Segmented<CurveUnit>
              label="Unidad de los valores del fichero"
              value={unit}
              onChange={onUnitChange}
              options={[
                { value: "kWh", label: "kWh por hora" },
                { value: "MWh", label: "MWh por hora" },
              ]}
            />
          </div>
          <label htmlFor={inputId} className="mt-3 inline-flex cursor-pointer items-center gap-1.5 rounded text-[12.5px] text-ink-2 underline decoration-line-strong underline-offset-4 hover:text-ink has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-paper">
            <Upload aria-hidden size={14} />
            Sustituir por otro fichero
            {picker}
          </label>
        </div>
      ) : (
        <label
          htmlFor={inputId}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
          className={`flex cursor-pointer flex-col items-center gap-1.5 rounded-md border border-dashed px-3 py-4 text-center transition-colors has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-paper ${
            dragging ? "border-paper bg-raised" : "border-line-strong bg-desk hover:border-ink-3 hover:bg-raised/60"
          }`}
        >
          <Upload aria-hidden size={18} className={dragging ? "text-paper" : "text-ink-2"} />
          <span className="text-[13px] font-semibold text-ink">{reading ? "Leyendo el fichero…" : dragging ? "Suelta el CSV aquí" : "Arrastra un CSV o elige un fichero"}</span>
          <span className="text-[12px] leading-snug text-ink-3">{fmt.n(expectedHours, 0)} valores, uno por hora, desde el 1 de enero a las 00:00</span>
          {picker}
        </label>
      )}

      {error && (
        <div className="mt-2">
          <Notice tone="error">{error}</Notice>
        </div>
      )}

      <p id={hintId} className="mt-2 text-[12px] leading-snug text-ink-3">
        {curve
          ? "La cotización usa la forma de tu curva en lugar del arquetipo y la escala al consumo anual indicado debajo."
          : "El consumo va en la última columna; se admiten cabecera, columnas de fecha y coma o punto decimal."}
      </p>
      <button
        type="button"
        onClick={() => downloadSampleCurve(year ?? 2025)}
        className="mt-2 inline-flex items-center gap-1.5 rounded text-[12.5px] text-ink-2 underline decoration-line-strong underline-offset-4 hover:text-ink"
      >
        <Download aria-hidden size={14} />
        Descargar un CSV de ejemplo
      </button>
    </div>
  );
}
