import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The frontend talks only to the gateway (services/gateway). In dev, Vite forwards these paths to it.
const gateway = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";
const rest = Object.fromEntries(["/api", "/sessions", "/detectors", "/streams"].map((p) => [p, gateway]));

export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1500 }, // kB; milsymbol alone is about 1 MB and the app is served on the LAN
  server: { proxy: { ...rest, "/ws": { target: gateway, ws: true } } },
});
