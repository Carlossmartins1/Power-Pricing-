"use client";

import { Activity, useEffect, useState, type ComponentType } from "react";
import { BriefcaseBusiness, Flame, Radar, Zap, type LucideProps } from "lucide-react";

import GasPricerTab from "@/components/GasPricerTab";
import MarketIntelTab from "@/components/MarketIntelTab";
import PortfolioBacktestTab from "@/components/PortfolioBacktestTab";
import PowerPricerTab from "@/components/PowerPricerTab";
import { Notice, fmt } from "@/components/ui";
import { API_BASE_URL, getHealth, type HealthResponse } from "@/lib/api";

type TabId = "power" | "gas" | "portfolio" | "market";

const TABS: { id: TabId; label: string; icon: ComponentType<LucideProps>; view: ComponentType }[] = [
  { id: "power", label: "Cotizador eléctrico", icon: Zap, view: PowerPricerTab },
  { id: "gas", label: "Cotizador de gas", icon: Flame, view: GasPricerTab },
  { id: "portfolio", label: "Cartera y P&L", icon: BriefcaseBusiness, view: PortfolioBacktestTab },
  { id: "market", label: "Mercado ibérico", icon: Radar, view: MarketIntelTab },
];

const HEALTH_POLL_MS = 20_000;

type Connection = { status: "checking" } | { status: "online"; health: HealthResponse } | { status: "offline" };

function useConnection(): Connection {
  const [connection, setConnection] = useState<Connection>({ status: "checking" });
  useEffect(() => {
    let cancelled = false;
    const check = () =>
      getHealth()
        .then((health) => !cancelled && setConnection({ status: "online", health }))
        .catch(() => !cancelled && setConnection({ status: "offline" }));
    void check();
    const timer = setInterval(check, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);
  return connection;
}

function ConnectionStatus({ connection }: { connection: Connection }) {
  const online = connection.status === "online";
  const label = { checking: "Comprobando la API", online: "API conectada", offline: "Sin conexión con la API" }[connection.status];
  const dot = { checking: "bg-ink-3", online: "bg-s6", offline: "bg-loss" }[connection.status];
  return (
    <dl className="flex flex-wrap items-stretch rounded-md border border-line bg-panel text-[12.5px]" aria-live="polite">
      <div className="flex items-center gap-2 px-3 py-2">
        <span aria-hidden className={`inline-block size-2 rounded-full ${dot}`} />
        <dt className="sr-only">Estado</dt>
        <dd className="font-semibold text-ink">{label}</dd>
      </div>
      {online && (
        <>
          <div className="border-l border-line px-3 py-2">
            <dt className="text-ink-3">Año de mercado</dt>
            <dd className="font-semibold text-ink">{connection.health.market_year}</dd>
          </div>
          <div className="border-l border-line px-3 py-2">
            <dt className="text-ink-3">Origen de datos</dt>
            <dd className="font-semibold text-ink">{connection.health.power.source === "SYNTHETIC" ? "Mercado sintético" : `${connection.health.power.source} y ${connection.health.gas.source}`}</dd>
          </div>
          <div className="border-l border-line px-3 py-2">
            <dt className="text-ink-3">Warehouse</dt>
            <dd className="font-semibold text-ink">
              {fmt.n(connection.health.power.rows, 0)} h, {connection.health.gas.rows} días
            </dd>
          </div>
        </>
      )}
    </dl>
  );
}

export default function Home() {
  const [tab, setTab] = useState<TabId>("power");
  const connection = useConnection();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-line bg-panel/40">
        <div className="mx-auto w-full max-w-[1720px] px-5 pt-6 md:px-8">
          <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
            <div className="max-w-[70ch]">
              <h1 className="text-[27px] font-semibold leading-tight text-ink">Mesa de pricing cuantitativo</h1>
              <p className="mt-1 text-[13.5px] leading-snug text-ink-2">
                Tarificación de electricidad (OMIE, REE) y gas (MIBGAS, Enagás) con riesgo de perfil y de desvíos para una comercializadora ibérica.
              </p>
            </div>
            <ConnectionStatus connection={connection} />
          </div>
          <nav className="mt-5 flex gap-1 overflow-x-auto" role="tablist" aria-label="Módulos de la mesa">
            {TABS.map(({ id, label, icon: Icon }) => {
              const active = id === tab;
              return (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  id={`tab-${id}`}
                  aria-selected={active}
                  aria-controls={`panel-${id}`}
                  onClick={() => setTab(id)}
                  className={`-mb-px flex items-center gap-2 whitespace-nowrap border-b-2 px-3.5 pb-3 pt-2 text-[14px] transition-colors ${active ? "border-paper font-semibold text-ink" : "border-transparent text-ink-2 hover:text-ink"}`}
                >
                  <Icon size={16} strokeWidth={active ? 2.2 : 1.8} aria-hidden />
                  {label}
                </button>
              );
            })}
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-[1720px] flex-1 px-5 py-6 md:px-8">
        {connection.status === "offline" && (
          <div className="mb-5">
            <Notice tone="error">
              No hay respuesta de la API en {API_BASE_URL}. Arranca el backend desde la carpeta backend con «uvicorn app.main:app --port 8000»; la mesa se reconecta sola.
            </Notice>
          </div>
        )}
        {TABS.map(({ id, view: View }) => (
          <Activity key={id} mode={id === tab ? "visible" : "hidden"}>
            <section role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`}>
              <View />
            </section>
          </Activity>
        ))}
      </main>

      <footer className="border-t border-line">
        <p className="mx-auto w-full max-w-[1720px] px-5 py-4 text-[12px] leading-snug text-ink-3 md:px-8">
          Proyecto personal de candidatura a Power Pricing Expert en MET Group. No es un producto ni una herramienta oficial de MET Group; las cifras salen del Data Warehouse local y no son precios de oferta.
        </p>
      </footer>
    </div>
  );
}
