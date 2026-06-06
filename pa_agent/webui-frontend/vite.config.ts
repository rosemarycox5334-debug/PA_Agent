import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';

// Vite + Vue 3 + TypeScript configuration for the PA Agent WebUI.
// - The app is served by the FastAPI backend (pa_agent/web/main.py) at /webui,
//   so we set `base` to '/webui/' to keep asset URLs inside the same mounted
//   StaticFiles directory as the SPA index.
// - During local development the dev server runs on :5173 and proxies /api
//   calls to the FastAPI process on :8080 so the SPA and API share a single
//   origin from the browser's perspective.
export default defineConfig({
  plugins: [vue()],
  base: '/webui/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
  },
});
