import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';
import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';

export default defineConfig({
  root: 'static-app',
  base: '/relay/',
  css: { postcss: { plugins: [tailwindcss()] } },
  plugins: [react()],
  server: {
    proxy: {
      '/relay/api': 'http://127.0.0.1:18777',
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('.', import.meta.url)),
    },
  },
  build: {
    outDir: '../static-dist',
    emptyOutDir: true,
  },
});
