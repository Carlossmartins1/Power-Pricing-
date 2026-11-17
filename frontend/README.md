# Dashboard: mesa de pricing cuantitativo

Next.js (App Router) + TypeScript + Tailwind CSS + Recharts + lucide-react.

```bash
cp .env.local.example .env.local   # opcional: por defecto apunta a http://localhost:8000
npm ci
npm run dev                        # http://localhost:3000
npm run build && npm run start     # build de producción
npm run lint
npm run typecheck
```

El backend debe estar levantado (`uvicorn app.main:app --port 8000` desde `backend/`).

| Archivo | Contenido |
|---|---|
| `src/lib/api.ts` | Cliente tipado de la API (`/api/v1`) |
| `src/components/ui.tsx` | Tokens de color, formato numérico, controles, tablas y piezas de gráfico compartidas |
| `src/components/PowerPricerTab.tsx` | Cotizador eléctrico |
| `src/components/CurveUpload.tsx`, `src/lib/loadCurve.ts` | Subida y validación del CSV de curva horaria y CSV de ejemplo |
| `src/components/GasPricerTab.tsx` | Cotizador de gas |
| `src/components/PortfolioBacktestTab.tsx` | Back-test de cartera, waterfall de P&L y alertas de repricing |
| `src/components/MarketIntelTab.tsx` | Inteligencia de mercado ibérico |
| `src/app/page.tsx`, `src/app/layout.tsx`, `src/app/globals.css` | Estructura, pestañas, estado de la API y tema |
