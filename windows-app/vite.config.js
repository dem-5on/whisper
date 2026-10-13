import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    // Rust sources and generated Cargo/Tauri files are not frontend sources.
    // In particular, Windows locks build executables and generated schemas
    // while compiling, which can crash Vite's recursive watcher with EBUSY.
    watch: { ignored: ["**/src-tauri/**"] },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    rollupOptions: { input: ["index.html", "keys.html", "pill.html", "home.html"] },
  },
});
