// Live status over WS /ws with automatic reconnect (exponential backoff, capped).
// `connected` is false while (re)connecting, so the UI can show it plainly.

import { useEffect, useRef, useState } from "react";
import { wsUrl } from "./client";
import type { Status } from "./types";

export interface SocketState {
  status: Status | null;
  connected: boolean;
  lastMessageAt: number | null;
  attempts: number;
}

export interface SocketOptions {
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** A connection that sends nothing for this long is treated as dead. */
  staleAfterMs?: number;
  factory?: (url: string) => WebSocket;
}

const POLICY_VIOLATION = 1008; // bad token: don't hammer the server

export function useStatusSocket(token: string | null, opts: SocketOptions = {}): SocketState {
  const { baseDelayMs = 1000, maxDelayMs = 15000, staleAfterMs = 5000 } = opts;
  const factory = opts.factory ?? ((url: string) => new WebSocket(url));
  const factoryRef = useRef(factory);
  useEffect(() => {
    factoryRef.current = factory;
  });
  const [state, setState] = useState<SocketState>({
    status: null,
    connected: false,
    lastMessageAt: null,
    attempts: 0,
  });

  useEffect(() => {
    if (!token) return;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let lastMessage = 0;
    let closed = false;

    const schedule = () => {
      if (closed) return;
      const delay = Math.min(baseDelayMs * 2 ** attempts, maxDelayMs);
      attempts += 1;
      setState((s) => ({ ...s, connected: false, attempts }));
      retry = setTimeout(connect, delay);
    };

    const connect = () => {
      if (closed) return;
      const sock = factoryRef.current(wsUrl(token));
      ws = sock;
      sock.onopen = () => {
        lastMessage = Date.now();
      };
      sock.onmessage = (ev: MessageEvent<string>) => {
        lastMessage = Date.now();
        let msg: { type?: string; data?: Status };
        try {
          msg = JSON.parse(ev.data) as { type?: string; data?: Status };
        } catch {
          return;
        }
        if (msg.type === "status" && msg.data) {
          attempts = 0;
          const data = msg.data;
          setState({ status: data, connected: true, lastMessageAt: lastMessage, attempts: 0 });
        }
      };
      sock.onclose = (ev: CloseEvent) => {
        if (ws !== sock) return;
        ws = null;
        if (ev.code === POLICY_VIOLATION) {
          closed = true;
          setState((s) => ({ ...s, connected: false }));
          return;
        }
        schedule();
      };
      sock.onerror = () => {
        sock.close();
      };
    };

    const watchdog = setInterval(() => {
      if (ws && lastMessage && Date.now() - lastMessage > staleAfterMs) {
        const dead = ws;
        ws = null;
        dead.close();
        schedule();
      }
    }, Math.max(staleAfterMs / 2, 250));

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(watchdog);
      ws?.close();
    };
  }, [token, baseDelayMs, maxDelayMs, staleAfterMs]);

  return token ? state : { status: null, connected: false, lastMessageAt: null, attempts: 0 };
}
