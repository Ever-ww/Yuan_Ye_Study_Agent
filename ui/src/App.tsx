import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AgentPage } from "./features/agent/AgentPage";

const CapabilitiesPage = lazy(() => import("./features/capabilities/CapabilitiesPage").then((module) => ({ default: module.CapabilitiesPage })));
const CodePage = lazy(() => import("./features/code/CodePage").then((module) => ({ default: module.CodePage })));
const OperationsPage = lazy(() => import("./features/operations/OperationsPage").then((module) => ({ default: module.OperationsPage })));
const ReaderPage = lazy(() => import("./features/reader/ReaderPage").then((module) => ({ default: module.ReaderPage })));
const WritePage = lazy(() => import("./features/writer/WritePage").then((module) => ({ default: module.WritePage })));

export default function App() {
  return (
    <Suspense fallback={<div className="boot-state"><div className="boot-mark">YY</div><h1>正在加载工作区</h1></div>}>
      <Routes>
        <Route path="/agent" element={<AgentPage />} />
        <Route path="/code" element={<CodePage />} />
        <Route path="/read" element={<ReaderPage />} />
        <Route path="/write" element={<WritePage />} />
        <Route path="/operations" element={<OperationsPage />} />
        <Route path="/capabilities" element={<CapabilitiesPage />} />
        <Route path="*" element={<Navigate replace to="/agent" />} />
      </Routes>
    </Suspense>
  );
}
