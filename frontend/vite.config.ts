/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the backend runs on :8000. The browser talks to /api and /ws on
// the Vite server, as it will talk to nginx in production (same origin, no CORS).
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env;
const backend = env?.BACKEND_URL ?? "http://127.0.0.1:8000"; // IPv4: Node may resolve localhost to ::1

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  build: { sourcemap: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
