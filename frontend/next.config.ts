import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // La imagen Docker compila con NEXT_OUTPUT=standalone y arranca .next/standalone/server.js (servidor mínimo con
  // sus dependencias trazadas). En local se deja sin definir para que `npm run start` siga funcionando.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
