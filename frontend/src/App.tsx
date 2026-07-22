import { Navigate, Route, Routes } from "react-router-dom";
import { useLocation, useParams } from "react-router-dom";
import { Shell } from "./ui";
import { AuthProvider, useAuth } from "./auth";
import { Overview } from "./pages/Overview";
import { Targets } from "./pages/Targets";
import { Suites } from "./pages/Suites";
import { Runs } from "./pages/Runs";
import { NewRun } from "./pages/NewRun";
import { RunDetail } from "./pages/RunDetail";
import { AgentWorkbench } from "./pages/AgentWorkbench";
import { Monitoring } from "./pages/Monitoring";
import { ForgotPasswordPage, LoginPage, RegisterPage, ResetPasswordPage, VerifyEmailPage } from "./pages/AuthPages";
import { CredentialsPage, ProviderDirectoryPage } from "./pages/ApiKeyManagement";
import { ApiKeyCatalogProbe } from "./pages/ApiKeyCatalogProbe";
import { PartnerOnboarding } from "./pages/PartnerOnboarding";
import { AccountSettings } from "./pages/AccountSettings";
import { EvaluationDatasets } from "./pages/EvaluationDatasets";
import { EvaluationScorers } from "./pages/EvaluationScorers";
import { EvaluationRuns } from "./pages/EvaluationRuns";

export function RootApp() {
  return <AuthProvider><AuthGate /></AuthProvider>;
}

function AuthGate() {
  const auth = useAuth();
  const location = useLocation();
  if (auth.phase === "loading") {
    return <div className="session-skeleton" aria-label="正在加载当前用户"><i /><div><span /><span /><span /></div></div>;
  }
  if (auth.phase === "error") {
    return <div className="session-failure"><RadioSignal /><h1>无法确认当前会话</h1><p>服务端暂时不可用，敏感页面尚未加载。</p><button onClick={auth.retry}>重新连接</button></div>;
  }
  if (auth.phase === "anonymous") {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/verify-email" element={<VerifyEmailPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />
        <Route path="*" element={<Navigate to="/login" replace state={{ from: location.pathname + location.search }} />} />
      </Routes>
    );
  }
  return <App />;
}

function RadioSignal() {
  return <span className="failure-signal" aria-hidden="true"><i /><i /><i /></span>;
}

export function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Navigate to="/overview" replace />} />
        <Route path="/overview" element={<Overview />} />
        <Route path="/api-keys" element={<Navigate to="/api-keys/credentials" replace />} />
        <Route path="/api-keys/credentials" element={<CredentialsPage />} />
        <Route path="/api-keys/channels" element={<Targets />} />
        <Route path="/api-keys/vendors" element={<ProviderDirectoryPage mode="vendors" />} />
        <Route path="/api-keys/sources" element={<ProviderDirectoryPage mode="sources" />} />
        <Route path="/probes/single" element={<Navigate to="/probes/single/new" replace />} />
        <Route path="/probes/single/new" element={<NewRun />} />
        <Route path="/probes/single/history" element={<Runs />} />
        <Route path="/probes/api-key" element={<ApiKeyCatalogProbe />} />
        <Route path="/probes/api-key/:batchId" element={<ApiKeyCatalogProbe />} />
        <Route path="/partners/onboarding" element={<PartnerOnboarding />} />
        <Route path="/assistant" element={<AgentWorkbench />} />
        <Route path="/assistant/:sessionId" element={<AgentWorkbench />} />
        <Route path="/partners/monitoring" element={<Monitoring />} />
        <Route path="/login" element={<Navigate to="/overview" replace />} />
        <Route path="/register" element={<Navigate to="/overview" replace />} />
        <Route path="/verify-email" element={<Navigate to="/overview" replace />} />
        <Route path="/forgot-password" element={<Navigate to="/overview" replace />} />
        <Route path="/reset-password" element={<Navigate to="/overview" replace />} />
        <Route path="/settings/profile" element={<AccountSettings section="profile" />} />
        <Route path="/settings/security" element={<AccountSettings section="security" />} />
        <Route path="/settings/workspace" element={<AccountSettings section="workspace" />} />
        <Route path="/agent" element={<Navigate to="/assistant" replace />} />
        <Route path="/agent/:sessionId" element={<LegacyAgentRedirect />} />
        <Route path="/monitoring" element={<Navigate to="/partners/monitoring" replace />} />
        <Route path="/targets" element={<Navigate to="/api-keys/channels" replace />} />
        <Route path="/suites" element={<Suites />} />
        <Route path="/suites/datasets" element={<EvaluationDatasets />} />
        <Route path="/suites/datasets/:datasetId" element={<EvaluationDatasets />} />
        <Route path="/suites/scorers" element={<EvaluationScorers />} />
        <Route path="/suites/evaluation-runs" element={<EvaluationRuns />} />
        <Route path="/suites/evaluation-runs/:evaluationRunId" element={<EvaluationRuns />} />
        <Route path="/runs" element={<Navigate to="/probes/single/history" replace />} />
        <Route path="/runs/new" element={<LegacyNewRunRedirect />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}

function LegacyAgentRedirect() {
  const { sessionId } = useParams();
  return <Navigate to={sessionId ? `/assistant/${sessionId}` : "/assistant"} replace />;
}

function LegacyNewRunRedirect() {
  const location = useLocation();
  return <Navigate to={`/probes/single/new${location.search}`} replace />;
}
