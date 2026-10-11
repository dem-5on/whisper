import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    // Cargo creates and replaces locked executables under target while Tauri
    // is compiling. They are not frontend sources and can crash Vite's watcher
    // on Windows if watched recursively.
    watch: { ignored: ["**/src-tauri/target/**"] },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    rollupOptions: { input: ["index.html", "keys.html"] },
  },
});
