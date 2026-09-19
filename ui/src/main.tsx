import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AppErrorBoundary } from "./app/AppErrorBoundary";
import { ThemeProvider } from "./app/ThemeProvider";
import { GatewayApi } from "./api";
import { GatewayApiProvider } from "./shared/api/context";
import "./styles/reset.css";
import "./styles/tokens.css";
import "./styles/workbench.css";

const root = ReactDOM.createRoot(document.getElementById("root")!);
const api = new GatewayApi();
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});

root.render(<BootState label="正在连接本机 Gateway…" />);

api.initialize().then(() => {
  root.render(
    <React.StrictMode>
      <AppErrorBoundary>
        <GatewayApiProvider api={api}>
          <QueryClientProvider client={queryClient}>
            <ThemeProvider>
              <BrowserRouter><App /></BrowserRouter>
            </ThemeProvider>
          </QueryClientProvider>
        </GatewayApiProvider>
      </AppErrorBoundary>
    </React.StrictMode>,
  );
}).catch((reason: unknown) => {
  root.render(<BootState error={reason instanceof Error ? reason.message : String(reason)} label="无法连接 Gateway" />);
});

function BootState({ label, error }: { label: string; error?: string }) {
  return <main className="boot-state"><div className="boot-mark">YY</div><h1>{label}</h1>{error && <p>{error}</p>}</main>;
}
