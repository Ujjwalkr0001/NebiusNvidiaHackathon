import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/testSetup.ts'],
    clearMocks: true,
  },
  server: {
    host: '127.0.0.1',
    allowedHosts: ['courier-blue-wayne-orange.trycloudflare.com'],
    proxy: { '/api': 'http://127.0.0.1:8001', '/health': 'http://127.0.0.1:8001' },
  },
});
