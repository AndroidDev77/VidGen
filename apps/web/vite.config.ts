import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Local development prefers this proxy over CORS: the browser talks to the Vite
 * origin only, so the API keeps cross-origin access disabled by default.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@vidgen/contracts": fileURLToPath(
        new URL("../../packages/ts-contracts/src/index.ts", import.meta.url),
      ),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VIDGEN_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
