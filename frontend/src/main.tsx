import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
// Imported for its side effect: i18next must be initialised before the first
// component calls useTranslation, and before React paints a single label.
import './i18n'
import './styles/index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Counts change while you are looking at them — another operator scanning
      // the same truck, Ops resolving a held box. Refetching on focus is what
      // keeps a tablet left on a shelf for ten minutes from lying to whoever
      // picks it up.
      refetchOnWindowFocus: true,
      staleTime: 5_000,
      retry: (failureCount, error) => {
        const status = (error as { status?: number })?.status ?? 0
        // 4xx means the server decided something — retrying will not change its
        // mind, and a control-point refusal must reach the operator immediately.
        if (status >= 400 && status < 500) return false
        return failureCount < 2
      },
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
