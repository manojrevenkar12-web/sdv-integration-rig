import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  test: { environment: "jsdom", include: ["test/**/*.test.tsx"] },
} as never);
