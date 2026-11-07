// One WebSocket for the whole app; pages read the latest status from here.
// While the socket is down, the status falls back to polling GET /status.

import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
import { api } from "../api/client";
import { getToken, onTokenChange } from "../api/token";
import type { Status } from "../api/types";
import { useStatusSocket } from "../api/useStatusSocket";

interface Live {
  status: Status | null;
  connected: boolean;
  lastMessageAt: number | null;
}

const Ctx = createContext<Live>({ status: null, connected: false, lastMessageAt: null });

export function useAdminToken(): string | null {
  return useSyncExternalStore(onTokenChange, getToken, () => null);
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const token = useAdminToken();
  const socket = useStatusSocket(token);
  const fallback = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    enabled: token !== null && !socket.connected,
    refetchInterval: 3000,
  });
  const value: Live = {
    status: socket.connected ? socket.status : (fallback.data ?? socket.status),
    connected: socket.connected,
    lastMessageAt: socket.lastMessageAt,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLive(): Live {
  return useContext(Ctx);
}
