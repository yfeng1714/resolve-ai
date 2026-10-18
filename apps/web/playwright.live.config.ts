import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./live",
  timeout: 180_000,
  fullyParallel: false,
  workers: 1,
  outputDir: "../../eval/results/m2-luna-rereview/browser",
  use: {
    baseURL: "http://127.0.0.1:15173",
    video: "on",
    trace: "on",
    screenshot: "on",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
