import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@fontsource-variable/archivo/wdth.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mesa de pricing cuantitativo | Electricidad y gas ibérico",
  description:
    "Cotización de electricidad (OMIE/REE) y gas (MIBGAS/Enagás), back-testing de cartera y atribución de P&L. Proyecto de candidatura a Power Pricing Expert en MET Group.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="es" className="h-full">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
