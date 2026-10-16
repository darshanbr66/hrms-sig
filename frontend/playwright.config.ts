import { defineConfig, devices } from "@playwright/test";

// Browser checks that jsdom cannot make (layout, scrolling, viewport sizes). The SPA runs on
// its own Vite server so it never touches the developer's server on 5173; every API call is
// answered by the test with fake data, so no database, account or authenticator is involved.
const PORT = 5180;

export default defineConfig({
  testDir: "e2e",
  testMatch: "**/*.e2e.ts",
  fullyParallel: true,
  workers: 2,
  timeout: 60_000,
  // Pages load on demand and the dev server compiles each on its first visit, which can take
  // several seconds while two workers run.
  expect: { timeout: 15_000 },
  forbidOnly: true,
  reporter: "list",
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chromium" } }],
  webServer: {
    command: `pnpm exec vite --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
