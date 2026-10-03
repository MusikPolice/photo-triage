import { fileURLToPath } from "node:url";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

const repoRoot = fileURLToPath(new URL("..", import.meta.url));
const DEFAULT_APP_PORT = "8000"; // matches Settings.app_port

/** Where the dev server sends `/api`: the API on APP_PORT, read like the backend reads it. */
export function apiTarget(env: Record<string, string>): string {
  return `http://127.0.0.1:${env["APP_PORT"] || DEFAULT_APP_PORT}`;
}

export default defineConfig(({ mode }) => ({
  plugins: [svelte()],
  server: {
    proxy: {
      // Real environment variables override the repo-root .env, as for the API.
      "/api": apiTarget(loadEnv(mode, repoRoot, "APP_PORT")),
    },
  },
  test: {
    include: ["tests/**/*.test.ts"],
  },
}));
