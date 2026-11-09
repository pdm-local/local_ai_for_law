import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../static/chat',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'chat.js',
        chunkFileNames: 'chat-[hash].js',
        assetFileNames: 'chat.[ext]',
      },
    },
  },
  server: {
    port: 5174,
    host: '0.0.0.0',
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
