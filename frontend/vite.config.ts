import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync, existsSync } from 'node:fs';

const releaseFile = new URL('./public/release.txt', import.meta.url);
const artifactRelease = existsSync(releaseFile) ? readFileSync(releaseFile, 'utf8').trim() : '';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'build',
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
          headless: ['@headlessui/react'],
          icons: ['@heroicons/react/24/outline', '@heroicons/react/24/solid'],
        },
      },
    },
  },
  define: {
    'process.env.REACT_APP_API_URL': JSON.stringify(process.env.REACT_APP_API_URL || ''),
    'process.env.REACT_APP_WS_URL': JSON.stringify(process.env.REACT_APP_WS_URL || ''),
    'process.env.REACT_APP_RELEASE': JSON.stringify(process.env.VERCEL_GIT_COMMIT_SHA || artifactRelease || 'development'),
  },
});
