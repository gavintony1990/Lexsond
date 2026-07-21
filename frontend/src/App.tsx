import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./ui";
import { Overview } from "./pages/Overview";
import { Targets } from "./pages/Targets";
import { Suites } from "./pages/Suites";
import { Runs } from "./pages/Runs";
import { NewRun } from "./pages/NewRun";
import { RunDetail } from "./pages/RunDetail";
import { AgentWorkbench } from "./pages/AgentWorkbench";
import { Monitoring } from "./pages/Monitoring";

export function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/agent" element={<AgentWorkbench />} />
        <Route path="/agent/:sessionId" element={<AgentWorkbench />} />
        <Route path="/monitoring" element={<Monitoring />} />
        <Route path="/targets" element={<Targets />} />
        <Route path="/suites" element={<Suites />} />
        <Route path="/runs" element={<Runs />} />
        <Route path="/runs/new" element={<NewRun />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
