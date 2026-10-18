import { defineConfig, devices } from "@playwright/test";

const root = "../..";
const origin = "http://127.0.0.1:15173";
const withTestEnv =
  'eval "$(PYTHONPATH=src .venv/bin/python -m resolveai.testing env)" && case "$DATABASE_URL" in */resolveai_pytest) ;; *) echo "refusing to start outside resolveai_pytest" >&2; exit 1 ;; esac';

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: origin,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  webServer: [
    {
      command:
        "PYTHONPATH=src UV_CACHE_DIR=.uv-cache .venv/bin/python -m resolveai.testing serve-simulator",
      cwd: root,
      url: "http://127.0.0.1:18081/health",
      reuseExistingServer: false,
      timeout: 60_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
    {
      command: `${withTestEnv} && UV_CACHE_DIR=.uv-cache uv run uvicorn resolveai.api.app:app --host 127.0.0.1 --port 18080 --app-dir src`,
      cwd: root,
      url: "http://127.0.0.1:18080/health",
      reuseExistingServer: false,
      timeout: 60_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
    {
      command: `${withTestEnv} && cd apps/web && ./node_modules/.bin/vite --host 127.0.0.1 --port 15173`,
      cwd: root,
      url: origin,
      reuseExistingServer: false,
      timeout: 30_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
  ],
});
