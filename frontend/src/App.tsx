import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { ApiError } from "./api/client";
import { Layout } from "./app/Layout";
import { LiveProvider, useAdminToken } from "./app/live";
import { ToastProvider, useToast } from "./app/toasts";
import { BacktestPage, BacktestsPage } from "./pages/Backtests";
import { Dashboard } from "./pages/Dashboard";
import { Login } from "./pages/Login";
import { OptionChainPage } from "./pages/OptionChain";
import { HelpPage } from "./pages/Help";
import { MorePage } from "./pages/More";
import { ActivityPage, DailyPnlPage, RiskPage } from "./pages/Reports";
import { SettingsPage } from "./pages/Settings";
import { StartPage } from "./pages/Start";
import { StrategyPage, Strategies } from "./pages/Strategies";

function message(e: unknown): string {
  if (e instanceof ApiError) return e.status ? `${e.message} (${e.status})` : e.message;
  return e instanceof Error ? e.message : "Something went wrong";
}

/** Every failed request or action surfaces as an error toast (401 just logs out). */
function useQueryClientWithToasts(): QueryClient {
  const toast = useToast();
  const [client] = useState(() => {
    const onError = (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return;
      toast.push(message(e), "error");
    };
    return new QueryClient({
      queryCache: new QueryCache({ onError }),
      mutationCache: new MutationCache({
        // Mutations with meta.handlesErrors show their own feedback (e.g. Settings' 409 dialog).
        onError: (e, _vars, _ctx, mutation) => {
          if (mutation.meta?.handlesErrors) return;
          onError(e);
        },
      }),
      defaultOptions: {
        queries: {
          retry: (n, e) => !(e instanceof ApiError && [401, 404, 422].includes(e.status)) && n < 2,
          refetchOnWindowFocus: true,
        },
        mutations: { retry: false },
      },
    });
  });
  return client;
}

/** After /auth/callback the backend redirects here with ?kite_login=success|failed. */
function KiteLoginNotice() {
  const toast = useToast();
  useEffect(() => {
    const url = new URL(window.location.href);
    const result = url.searchParams.get("kite_login");
    if (!result) return;
    if (result === "success") toast.push("Connected to Zerodha for today", "success");
    else toast.push(`Zerodha login failed: ${url.searchParams.get("reason") ?? "unknown"}`, "error");
    for (const k of ["kite_login", "reason", "user_id"]) url.searchParams.delete(k);
    window.history.replaceState(null, "", url.toString());
  }, [toast]);
  return null;
}

function OldRobot() {
  const { id = "" } = useParams();
  return <Navigate to={`/robots/${encodeURIComponent(id)}`} replace />;
}

function Routed() {
  const token = useAdminToken();
  const client = useQueryClientWithToasts();
  if (!token) return <Login />;
  return (
    <QueryClientProvider client={client}>
      <LiveProvider>
        <KiteLoginNotice />
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="start" element={<StartPage />} />
            <Route path="plan" element={<SettingsPage />} />
            <Route path="robots" element={<Strategies />} />
            <Route path="robots/:id" element={<StrategyPage />} />
            <Route path="activity" element={<ActivityPage />} />
            <Route path="history" element={<DailyPnlPage />} />
            <Route path="option-chain" element={<OptionChainPage />} />
            <Route path="backtests" element={<BacktestsPage />} />
            <Route path="backtests/:id" element={<BacktestPage />} />
            <Route path="safety" element={<RiskPage />} />
            <Route path="help" element={<HelpPage />} />
            <Route path="more" element={<MorePage />} />
            {/* Old addresses keep working. */}
            <Route path="settings" element={<Navigate to="/plan" replace />} />
            <Route path="strategies" element={<Navigate to="/robots" replace />} />
            <Route path="strategies/:id" element={<OldRobot />} />
            <Route path="orders" element={<Navigate to="/activity?tab=orders" replace />} />
            <Route path="trades" element={<Navigate to="/activity" replace />} />
            <Route path="pnl" element={<Navigate to="/history" replace />} />
            <Route path="risk" element={<Navigate to="/safety" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </LiveProvider>
    </QueryClientProvider>
  );
}

export function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routed />
      </BrowserRouter>
    </ToastProvider>
  );
}
