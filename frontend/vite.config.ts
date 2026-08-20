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
        // Deliberately excludes .wasm and .gz, which is what keeps the ~13MB
        // Tesseract runtime *out* of the precache manifest. Precaching it would
        // mean every guard's phone downloading an OCR engine during install to
        // run a scanner that never uses it.
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // The one exception to that: worker.min.js matches the glob above and
        // would otherwise be precached on its own — 128KB of a runtime whose
        // other 6MB isn't there.
        // Also excludes the pdf.js chunk. It is 445KB and only a matcher opening
        // a PDF challan ever needs it; precaching put it on every guard's phone at
        // install and undid the point of the dynamic import. Runtime-cached below
        // instead, on the same reasoning as the OCR engine.
        globIgnores: ['**/tesseract/**', '**/assets/pdf-*.js'],
        // API responses are never served from cache. A stale box count shown as
        // current is worse than no box count at all.
        navigateFallbackDenylist: [/^\/api/],
        // Raised from the 2MB default: the individual core .wasm is 3.1MB and
        // Workbox will refuse to cache a response larger than this.
        maximumFileSizeToCacheInBytes: 8 * 1024 * 1024,
        runtimeCaching: [
          {
            // The OCR runtime, cached on first use rather than at install.
            //
            // CacheFirst is right here in a way it would never be for API data:
            // these files are immutable for a given dependency version, so
            // "stale" is not a state they can be in. The matcher's station pays
            // the ~6MB download once and then reads challans offline forever;
            // the dashboard-only users never fetch it at all.
            urlPattern: /\/tesseract\/.*\.(wasm|mjs|js|gz)$|\/assets\/pdf-[^/]*\.js$/,
            handler: 'CacheFirst',
            options: {
              cacheName: 'ocr-runtime',
              expiration: { maxEntries: 12 },
              // Without this, an opaque cross-origin-style response could be
              // cached as a success and poison the engine until someone clears
              // site data by hand.
              cacheableResponse: { statuses: [200] },
            },
          },
        ],
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
