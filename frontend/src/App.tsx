import { useEffect, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { AuthProvider, useAuth } from '@/hooks/useAuth'
import { Layout } from '@/components/Layout'
import { Banner, Spinner } from '@/components/ui'
import { startAutoFlush } from '@/lib/offlineQueue'

import { LoginPage } from '@/pages/Login'
import { DashboardPage } from '@/pages/Dashboard'
import { ApprovalsPage } from '@/pages/Approvals'
import { GateEntryPage } from '@/pages/GateEntry'
import { EntriesPage } from '@/pages/Entries'
import { BoxCountingPage } from '@/pages/BoxCounting'
import { UnitScanningPage } from '@/pages/UnitScanning'
import { ExceptionsPage } from '@/pages/Exceptions'
import { ReconciliationPage } from '@/pages/Reconciliation'
import { PutawayPage } from '@/pages/Putaway'
import { InvoiceMatchingPage } from '@/pages/InvoiceMatching'
import { InvoicesPage } from '@/pages/Invoices'
import { PackingPage } from '@/pages/Packing'
import { BatchesPage } from '@/pages/Batches'
import { StockPage } from '@/pages/Stock'
import { PickupPage } from '@/pages/Pickup'
import { ReportsPage } from '@/pages/Reports'
import { AdminPage } from '@/pages/Admin'
import { LoadingPage } from '@/pages/Loading'

/**
 * Route guard.
 *
 * This decides what is *shown*. It is not what decides what is *allowed* —
 * every endpoint re-checks the role, and RLS re-checks it again in the
 * database. Hiding a page the API would refuse anyway is a courtesy to the
 * user, not a security boundary, and treating it as one is how gaps appear.
 */
function Protected({ page, children }: { page?: string; children: ReactNode }) {
  const { t } = useTranslation()
  const { session, me, loading, error, signOut } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  if (loading) return <Spinner label={t('app.signing_in')} />

  if (!session) return <Navigate to="/login" state={{ from: location }} replace />

  if (error || !me) {
    return (
      <div className="mx-auto max-w-lg space-y-4 p-6">
        <Banner tone="bad" title={t('app.no_profile_title')}>
          {t('app.no_profile_body')}
        </Banner>
        {/* The escape hatch this state was missing.
        
            The usual cause is a session that outlived the profile it points at —
            the database was re-seeded, ids regenerated, and the browser kept the
            old JWT. That is only fixable by signing out, and `Layout` (which owns
            the sign-out button) is not rendered here. So without this the user is
            stuck on this screen with no way forward at all. */}
        <button
          type="button"
          className="btn-primary w-full"
          onClick={async () => {
            await signOut()
            navigate('/login', { replace: true })
          }}
        >
          {t('common.sign_out')}
        </button>
      </div>
    )
  }

  if (page && !me.allowed_pages.includes(page)) {
    return (
      <Layout>
        <Banner tone="warn" title={t('app.wrong_role_title')}>
          {t('app.wrong_role_body', {
            role: t(`roles.${me.role}`, { defaultValue: me.role_label }),
          })}
        </Banner>
      </Layout>
    )
  }

  return <Layout>{children}</Layout>
}

/** Land each role on the page they actually work from. */
function Home() {
  const { me, loading, session } = useAuth()

  if (loading) return <Spinner />
  if (!session) return <Navigate to="/login" replace />

  const landing: Record<string, string> = {
    security_guard: '/gate-entry',
    admin: '/dashboard',
    offloading: '/entries',
    packer: '/packing',
  }

  return <Navigate to={landing[me?.role ?? ''] ?? '/entries'} replace />
}

function Shell() {
  useEffect(() => startAutoFlush(), [])

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<Home />} />

      <Route
        path="/dashboard"
        element={
          <Protected page="dashboard">
            <DashboardPage />
          </Protected>
        }
      />
      <Route
        path="/approvals"
        element={
          <Protected page="approvals">
            <ApprovalsPage />
          </Protected>
        }
      />
      <Route
        path="/gate-entry"
        element={
          <Protected page="gate-entry">
            <GateEntryPage />
          </Protected>
        }
      />
      <Route
        path="/entries"
        element={
          <Protected>
            <EntriesPage />
          </Protected>
        }
      />
      <Route
        path="/entries/:entryId/boxes"
        element={
          <Protected page="box-counting">
            <BoxCountingPage />
          </Protected>
        }
      />
      <Route
        path="/entries/:entryId/units"
        element={
          <Protected page="unit-scanning">
            <UnitScanningPage />
          </Protected>
        }
      />
      <Route
        path="/entries/:entryId/reconciliation"
        element={
          <Protected page="reconciliation">
            <ReconciliationPage />
          </Protected>
        }
      />
      <Route
        path="/putaway"
        element={
          <Protected page="putaway">
            <PutawayPage />
          </Protected>
        }
      />
      <Route
        path="/stock"
        element={
          <Protected page="stock">
            <StockPage />
          </Protected>
        }
      />
      <Route
        path="/invoices"
        element={
          <Protected page="invoices">
            <InvoicesPage />
          </Protected>
        }
      />
      <Route
        path="/invoice-matching"
        element={
          <Protected page="invoice-matching">
            <InvoiceMatchingPage />
          </Protected>
        }
      />
      <Route
        path="/packing"
        element={
          <Protected page="packing">
            <PackingPage />
          </Protected>
        }
      />
      <Route
        path="/batches"
        element={
          <Protected page="batches">
            <BatchesPage />
          </Protected>
        }
      />
      <Route
        path="/pickup"
        element={
          <Protected page="pickup">
            <PickupPage />
          </Protected>
        }
      />
      <Route
        path="/exceptions"
        element={
          <Protected page="exceptions">
            <ExceptionsPage />
          </Protected>
        }
      />
      <Route
        path="/reports"
        element={
          <Protected page="reports">
            <ReportsPage />
          </Protected>
        }
      />

      <Route
        path="/admin"
        element={
          <Protected page="admin">
            <AdminPage />
          </Protected>
        }
      />

      <Route
        path="/loading"
        element={
          <Protected page="loading">
            <LoadingPage />
          </Protected>
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  )
}
