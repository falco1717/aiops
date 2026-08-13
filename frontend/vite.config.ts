import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build lands directly in the FastAPI static directory so the
// API and the UI ship as one container.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/app/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.AIOPS_DEV_API ?? "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
