import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The SPA and the API share one origin (docs/architecture.md): in development Vite serves
// the SPA and forwards /api to the local API, so cookies and the CSRF origin check behave
// as they do behind the production proxy.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { sourcemap: false },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
    testTimeout: 20000,
    // Pages load on demand, so each file's first render compiles its page. With one worker per
    // core (13 files at once on a 12-thread machine) that first import sometimes took longer
    // than the 5 s wait in src/test/setup.ts, failing a different few tests on each run
    // (2026-10-19, docs/module-acceptance.md §13). Four workers keep it well inside the wait.
    maxWorkers: 4,
  },
});
