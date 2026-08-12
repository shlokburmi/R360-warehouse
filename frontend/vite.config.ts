import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import path from 'node:path'

export default defineConfig({
  plugins: [
    react(),
    // The gate and the offloading bay both have patchy wifi. The service worker
    // keeps the app shell available so a dropped connection means "scans are
    // queuing" rather than a blank browser error page.
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg'],
      manifest: {
        name: 'R360 Warehouse',
        short_name: 'Warehouse',
        description: 'Reward360 Warehouse Management',
        theme_color: '#0f172a',
        background_color: '#0f172a',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // API responses are never served from cache. A stale box count shown as
        // current is worse than no box count at all.
        navigateFallbackDenylist: [/^\/api/],
        runtimeCaching: [],
      },
    }),
  ],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    host: true, // reachable from a phone on the same network for real device testing
    // Proxy the API through the dev server so the browser only ever talks to
    // one origin.
    //
    // Talking straight to http://127.0.0.1:8000 works until it doesn't: recent
    // macOS gates page requests to loopback behind a Local Network privacy
    // prompt, `localhost` resolves to ::1 while uvicorn binds 127.0.0.1, and
    // testing from a phone on the LAN cannot reach the laptop's loopback at
    // all. Every one of those surfaces as a bare `fetch` rejection — which the
    // app can only report as "No connection", because that is all the browser
    // tells it.
    //
    // Proxying makes the request same-origin and side-steps the lot. Production
    // is unaffected: Vercel sets VITE_API_URL to the absolute Render URL and
    // this block does not exist in a built bundle.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    sourcemap: true,
    target: 'es2020',
  },
})
