import { Routes, Route, useLocation, Navigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { MotionConfig, motion } from 'framer-motion';
import { getMe } from './api/auth.js';
import Landing from './pages/Landing.jsx';
import Login from './pages/Login.jsx';
import Campaigns from './pages/Campaigns.jsx';
import CampaignCreate from './pages/CampaignCreate.jsx';
import CampaignDetail from './pages/CampaignDetail.jsx';
import Preview from './pages/Preview.jsx';
import Analytics from './pages/Analytics.jsx';
import SequenceBuilder from './pages/SequenceBuilder.jsx';
import Leads from './pages/Leads.jsx';
import Opportunities from './pages/Opportunities.jsx';
import Reports from './pages/Reports.jsx';
import ReportBuilder from './pages/ReportBuilder.jsx';
import Dashboard from './pages/Dashboard.jsx';
import UiKit from './pages/UiKit.jsx';
import OpportunityDetail from './pages/OpportunityDetail.jsx';
import Replies from './pages/Replies.jsx';
import ResearchClient from './pages/ResearchClient.jsx';
import Settings from './pages/Settings.jsx';
import Admin from './pages/Admin.jsx';
import Docs from './pages/Docs.jsx';
import Setup from './pages/Setup.jsx';
import Nav from './components/Nav.jsx';
import ErrorBoundary from './components/ErrorBoundary.jsx';
import { ToastProvider } from './components/Toast.jsx';
import NotificationBell from './components/NotificationBell.jsx';
import { spring } from './utils/motion.js';

/**
 * Routed content with a calm page-mount transition (Phase 4, UI refinement).
 * Keyed on the pathname so navigating between pages re-mounts + animates the
 * content in (fade + a few px rise) — non-blocking: there's no exit animation
 * to wait on, the new page renders immediately with its initial style and
 * settles via the shared spring.  Under `prefers-reduced-motion: reduce` the
 * MotionConfig at the root snaps it instantly.
 */
function RoutedContent() {
  const location = useLocation();
  return (
    <motion.div
      key={location.pathname}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={spring}
    >
      <Routes location={location}>
        <Route path="/campaigns" element={<Campaigns />} />
        <Route path="/campaigns/new" element={<CampaignCreate />} />
        <Route path="/campaigns/:id" element={<CampaignDetail />} />
        <Route path="/campaigns/:id/preview" element={<Preview />} />
        <Route path="/campaigns/:id/sequence" element={<SequenceBuilder />} />
        <Route path="/campaigns/:id/analytics" element={<Analytics />} />
        <Route path="/leads" element={<Leads />} />
        <Route path="/opportunities" element={<Opportunities />} />
        <Route path="/opportunities/:id" element={<OpportunityDetail />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/reports/builder" element={<ReportBuilder />} />
        <Route path="/replies" element={<Replies />} />
        <Route path="/research-client" element={<ResearchClient />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="/ui-kit" element={<UiKit />} />{/* internal design-system preview */}
      </Routes>
    </motion.div>
  );
}

/**
 * Auth gate (multi-tenancy Phase 1).  Every feature route lives behind it;
 * an unauthenticated visitor is bounced to /login.  The ['auth-me'] query
 * is the one source of session state — Login invalidates it after sign-in.
 */
function RequireAuth({ children }) {
  const { data: me, isLoading, isError } = useQuery({
    queryKey: ['auth-me'],
    queryFn: getMe,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  if (isLoading) {
    return (
      <div
        data-testid="auth-loading"
        className="min-h-screen bg-slate-50 flex items-center justify-center text-sm text-slate-400"
      >
        Loading…
      </div>
    );
  }
  if (isError || !me) return <Navigate to="/" replace />;
  return children;
}

function AppShell() {
  return (
    <div className="flex min-h-screen bg-slate-50">
      <Nav />
      <main className="flex-1 min-w-0 overflow-auto">
        <NotificationBell />
        <ErrorBoundary>
          <RoutedContent />
        </ErrorBoundary>
      </main>
    </div>
  );
}

export default function App() {
  return (
    // reducedMotion="user" → every framer-motion surface (modals, menus, tab
    // indicator, list mounts, this page transition) honors the OS
    // "reduce motion" setting automatically.
    <MotionConfig reducedMotion="user" transition={spring}>
      <ToastProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/docs" element={<Docs />} />
          <Route path="/setup" element={<RequireAuth><Setup /></RequireAuth>} />
          <Route
            path="/*"
            element={(
              <RequireAuth>
                <AppShell />
              </RequireAuth>
            )}
          />
        </Routes>
      </ToastProvider>
    </MotionConfig>
  );
}
