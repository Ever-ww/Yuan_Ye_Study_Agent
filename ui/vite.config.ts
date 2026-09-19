import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { host: "127.0.0.1", port: 1420, strictPort: true },
  build: {
    target: ["es2022", "chrome120", "safari14"],
    // Editor and PDF engines are intentionally lazy route chunks. Their
    // upstream parsers are large but do not affect the initial Agent route.
    chunkSizeWarningLimit: 750,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/@codemirror/")) return "editor";
          if (id.includes("node_modules/pdfjs-dist/") || id.includes("node_modules/react-pdf/")) return "pdf";
          if (id.includes("node_modules/@tanstack/")) return "tanstack";
          return undefined;
        },
      },
    },
  },
});
