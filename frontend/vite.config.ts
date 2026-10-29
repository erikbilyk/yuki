import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// Only the two port settings are read from the working copy's .env; no other value reaches this file.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, "..", ["WG_PORT", "WG_WEB_PORT"]), ...process.env };
  const backend = `http://127.0.0.1:${env.WG_PORT ?? "8100"}`;
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: Number(env.WG_WEB_PORT ?? "5170"),
      proxy: {
        "/v1": backend,
        "/sim": backend,
        "/healthz": backend,
      },
    },
    test: { environment: "node", include: ["src/**/*.test.ts"] },
  };
});
