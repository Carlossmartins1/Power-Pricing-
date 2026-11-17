/**
 * Curva horaria propia del cliente: lectura y validación del CSV, resumen y fichero de ejemplo.
 *
 * El motor alinea la curva con el calendario de mercado por posición: el valor i es la hora i del año,
 * contada desde el 1 de enero a las 00:00 (hora peninsular). Por eso hacen falta exactamente 8.760 valores
 * (8.784 en año bisiesto): el día del cambio de hora de marzo tiene 23 y el de octubre, 25.
 */

export const MAX_CSV_BYTES = 2 * 1024 * 1024;
const HOUR_MS = 3_600_000;
const HEADER_LINES_ALLOWED = 10;

export type CurveUnit = "kWh" | "MWh";

export interface LoadCurve {
  fileName: string;
  /** Un valor por hora, en la unidad del fichero. El motor solo usa la forma: la reescala al consumo anual. */
  values: number[];
  total: number;
  peak: number;
  /** Consumo medio de cada hora local del día (0-23), en la unidad del fichero. */
  hourlyMean: number[];
  headerLines: number;
}

/** Error de lectura con un mensaje que dice qué falla y cómo arreglarlo. */
export class CurveError extends Error {}

export const hoursInYear = (year: number): number => ((year % 4 === 0 && year % 100 !== 0) || year % 400 === 0 ? 8784 : 8760);

const integer = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0, useGrouping: "always" });

/** Último domingo de un mes a la 01:00 UTC: instante del cambio de hora en la UE. */
function lastSundayAt1Utc(year: number, monthIndex: number): number {
  const lastDay = new Date(Date.UTC(year, monthIndex + 1, 0, 1));
  return lastDay.getTime() - lastDay.getUTCDay() * 24 * HOUR_MS;
}

/** Hora peninsular de cada posición del año, como fecha UTC desplazada (leer con getUTC*). */
function localClock(year: number): Date[] {
  const start = Date.UTC(year - 1, 11, 31, 23); // 1 de enero, 00:00 CET
  const summerStart = lastSundayAt1Utc(year, 2);
  const summerEnd = lastSundayAt1Utc(year, 9);
  return Array.from({ length: hoursInYear(year) }, (_, index) => {
    const utc = start + index * HOUR_MS;
    const offset = utc >= summerStart && utc < summerEnd ? 2 : 1;
    return new Date(utc + offset * HOUR_MS);
  });
}

function toNumber(cell: string, decimalComma: boolean): number {
  let text = cell.trim().replace(/^"|"$/g, "").replace(/\s/g, "");
  if (text === "") return Number.NaN;
  if (decimalComma && text.includes(",")) text = text.replace(/\./g, "").replace(",", ".");
  return /^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(text) ? Number(text) : Number.NaN;
}

/**
 * Separador de columnas. Punto y coma o tabulador mandan (CSV español, con coma decimal). Si solo hay comas,
 * son separador cuando la primera columna no es un número suelto (fecha) o es un índice 1, 2, 3...; si no,
 * el fichero es una única columna con coma decimal.
 */
function detectDelimiter(lines: string[]): { delimiter: string | null; decimalComma: boolean } {
  const sample = lines.slice(0, 40);
  if (sample.some((line) => line.includes(";"))) return { delimiter: ";", decimalComma: true };
  if (sample.some((line) => line.includes("\t"))) return { delimiter: "\t", decimalComma: true };
  if (!sample.some((line) => line.includes(","))) return { delimiter: null, decimalComma: false };
  if (sample.some((line) => line.includes("."))) return { delimiter: ",", decimalComma: false };
  const firstCells = sample.map((line) => line.split(",")[0].trim());
  const numeric = firstCells.map((cell) => toNumber(cell, false)).filter((value) => Number.isFinite(value));
  const isIndex = numeric.length >= 3 && numeric.every((value, position) => position === 0 || value - numeric[position - 1] === 1);
  const hasLabels = firstCells.filter((cell) => !Number.isFinite(toNumber(cell, false))).length > 1;
  return isIndex || hasLabels ? { delimiter: ",", decimalComma: false } : { delimiter: null, decimalComma: true };
}

/**
 * Lee un CSV de curva horaria. Admite cabecera, columnas previas (fecha, hora, CUPS...) y coma o punto decimal:
 * el consumo es siempre la última columna con contenido de cada fila.
 */
export function parseLoadCurveCsv(text: string, fileName: string, year: number | null): LoadCurve {
  const lines = text
    .replace(/^\uFEFF/, "")
    .split(/\r\n|\n|\r/)
    .map((line, index) => ({ line: line.trim(), row: index + 1 }))
    .filter(({ line }) => line !== "");
  if (lines.length === 0) throw new CurveError("El fichero está vacío. Sube un CSV con el consumo de cada hora del año.");

  const { delimiter, decimalComma } = detectDelimiter(lines.map(({ line }) => line));
  const values: number[] = [];
  let headerLines = 0;
  for (const { line, row } of lines) {
    const cells = (delimiter ? line.split(delimiter) : [line]).filter((cell) => cell.trim() !== "");
    const value = toNumber(cells[cells.length - 1] ?? "", decimalComma);
    if (Number.isFinite(value)) {
      if (value < 0) {
        throw new CurveError(`La fila ${integer.format(row)} tiene un valor negativo. La curva debe ser el consumo de red de cada hora, sin excedentes vertidos.`);
      }
      values.push(value);
    } else if (values.length === 0 && headerLines < HEADER_LINES_ALLOWED) {
      headerLines += 1;
    } else if (values.length === 0) {
      throw new CurveError("No se ha encontrado ninguna columna numérica. El consumo de cada hora debe ir en la última columna.");
    } else {
      throw new CurveError(`La fila ${integer.format(row)} no tiene un consumo numérico en la última columna («${line.slice(0, 40)}»). Corrígela o elimínala.`);
    }
  }
  if (values.length === 0) throw new CurveError("No se ha encontrado ninguna columna numérica. El consumo de cada hora debe ir en la última columna.");

  const accepted = year === null ? [8760, 8784] : [hoursInYear(year)];
  if (!accepted.includes(values.length)) {
    const expected = year === null ? "8.760 (8.784 si el año es bisiesto)" : `${integer.format(accepted[0])}, uno por cada hora de ${year}`;
    const hint = values.length > accepted[0] ? "Quita filas de totales o duplicadas." : "Comprueba que no falten días ni horas.";
    throw new CurveError(`El fichero tiene ${integer.format(values.length)} valores horarios y hacen falta ${expected}. ${hint}`);
  }
  const total = values.reduce((sum, value) => sum + value, 0);
  if (total <= 0) throw new CurveError("Todos los valores son cero. La curva necesita consumo en alguna hora para poder perfilarla.");

  const clock = localClock(year ?? (values.length === 8784 ? 2024 : 2025));
  const sums = new Array<number>(24).fill(0);
  const counts = new Array<number>(24).fill(0);
  values.forEach((value, index) => {
    const hour = clock[index].getUTCHours();
    sums[hour] += value;
    counts[hour] += 1;
  });
  return {
    fileName,
    values: values.map((value) => Math.round(value * 1e6) / 1e6),
    total,
    peak: values.reduce((max, value) => Math.max(max, value), 0),
    hourlyMean: sums.map((sum, hour) => sum / counts[hour]),
    headerLines,
  };
}

/** Consumo anual que representa el fichero, en MWh, dentro de los límites que admite el cotizador. */
export function annualMwh(curve: LoadCurve, unit: CurveUnit, min: number, max: number): number {
  const mwh = unit === "kWh" ? curve.total / 1000 : curve.total;
  return Math.min(max, Math.max(min, Math.round(mwh * 1000) / 1000));
}

/** Horas equivalentes a plena carga sobre las horas del año: cuánto de plana es la curva. */
export const loadFactor = (curve: LoadCurve): number => (curve.peak > 0 ? curve.total / (curve.peak * curve.values.length) : 0);

const bump = (hour: number, center: number, width: number): number => {
  const distance = ((hour - center + 36) % 24) - 12;
  return Math.exp(-0.5 * (distance / width) ** 2);
};

/**
 * CSV de ejemplo: almacén frigorífico con actividad logística de lunes a viernes. Formato de exportación español
 * (punto y coma, coma decimal, hora local), con sus 8.760 filas y los dos cambios de hora.
 */
export function buildSampleCurveCsv(year: number): string {
  let seed = 20250101;
  const noise = (): number => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const pad = (value: number): string => String(value).padStart(2, "0");
  const rows = localClock(year).map((moment) => {
    const hour = moment.getUTCHours() + 0.5;
    const summer = 0.5 * (1 - Math.cos((2 * Math.PI * moment.getUTCMonth()) / 12));
    const weekday = moment.getUTCDay() >= 1 && moment.getUTCDay() <= 5;
    const activity = 1 / (1 + Math.exp(-(hour - 7) / 0.6)) / (1 + Math.exp((hour - 19) / 0.8));
    const compressors = 38 + 22 * summer + (10 + 12 * summer) * bump(hour, 15.5, 3.2);
    const kwh = (compressors + (weekday ? 30 : 8) * activity) * (0.96 + 0.08 * noise());
    const stamp = `${moment.getUTCFullYear()}-${pad(moment.getUTCMonth() + 1)}-${pad(moment.getUTCDate())} ${pad(moment.getUTCHours())}:00`;
    return `${stamp};${kwh.toFixed(3).replace(".", ",")}`;
  });
  return `\uFEFFfecha_hora_local;consumo_kwh\r\n${rows.join("\r\n")}\r\n`;
}

export function downloadSampleCurve(year: number): void {
  const url = URL.createObjectURL(new Blob([buildSampleCurveCsv(year)], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `curva_horaria_ejemplo_${year}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
