import {defineConfig} from "vite";

export default defineConfig({
  build: {
    rolldownOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@xyflow")) return "graph-vendor";
          if (id.includes("@astryxdesign")) return "design-system";
          if (id.includes("react")) return "react-vendor";
          return undefined;
        },
      },
    },
  },
});
