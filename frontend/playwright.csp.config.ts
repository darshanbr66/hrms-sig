import { defineConfig, devices } from "@playwright/test";

// The production build under the production headers (`pnpm test:csp`, docs/deployment.md §10).
// The everyday browser tests run on the Vite dev server, which serves the app differently
// (unbundled modules, injected styles), so the Content-Security-Policy in vercel.json can only
// be checked against `vite build` output. Every API call is answered with fake data.
const PORT = 5181;

export default defineConfig({
  testDir: "e2e",
  testMatch: "production-csp.e2e.ts",
  workers: 1,
  timeout: 60_000,
  forbidOnly: true,
  reporter: "list",
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chromium" } }],
  webServer: {
    command: `pnpm exec vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
