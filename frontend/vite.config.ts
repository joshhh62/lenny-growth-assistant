import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api and /health are proxied to the FastAPI backend so the UI can be
// served by Vite on :5173 without CORS configuration.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": { target: process.env.VITE_API_PROXY || "http://localhost:8000", changeOrigin: true },
      "/health": { target: process.env.VITE_API_PROXY || "http://localhost:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
