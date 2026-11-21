/* Playwright's end-to-end tests, against the stack `just e2e` starts
 * (scripts/e2e.sh), which passes its address as E2E_BASE_URL. */
import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env["E2E_BASE_URL"];
if (baseURL === undefined) throw new Error("Run these with `just e2e`, which sets E2E_BASE_URL");

export default defineConfig({
  testDir: ".",
  outputDir: "../test-results",
  // The worker processes the fixtures once for every test, so they share it.
  fullyParallel: false,
  workers: 1,
  forbidOnly: process.env["CI"] !== undefined,
  retries: 0,
  reporter: process.env["CI"] !== undefined ? [["list"], ["github"]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
