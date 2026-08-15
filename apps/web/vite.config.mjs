import {defineConfig} from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [tailwindcss()],
  build: {
    rolldownOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@xyflow")) return "graph-vendor";
          if (id.includes("@cloudflare/kumo")) return "design-system";
          if (id.includes("react")) return "react-vendor";
          return undefined;
        },
      },
    },
  },
});
