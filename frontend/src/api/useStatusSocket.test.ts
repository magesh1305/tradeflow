import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Status } from "./types";
import { useStatusSocket } from "./useStatusSocket";

class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({ code: 1000 });
  }

  // test helpers
  open() {
    this.onopen?.();
  }
  send(status: Partial<Status>) {
    this.onmessage?.({ data: JSON.stringify({ type: "status", data: status }) });
  }
  drop(code = 1006) {
    this.closed = true;
    this.onclose?.({ code });
  }
}

const factory = (url: string) => new FakeSocket(url) as unknown as WebSocket;
const last = () => FakeSocket.all[FakeSocket.all.length - 1]!;

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("useStatusSocket", () => {
  it("does nothing without a token", () => {
    const { result } = renderHook(() => useStatusSocket(null, { factory }));
    expect(FakeSocket.all).toHaveLength(0);
    expect(result.current.connected).toBe(false);
  });

  it("connects with the token and exposes live status", () => {
    const { result } = renderHook(() => useStatusSocket("tok-123", { factory }));
    expect(last().url).toContain("/ws?token=tok-123");
    act(() => {
      last().open();
      last().send({ mode: "paper", engine_running: true });
    });
    expect(result.current.connected).toBe(true);
    expect(result.current.status?.mode).toBe("paper");
  });

  it("shows disconnected and reconnects with growing backoff", () => {
    const { result } = renderHook(() => useStatusSocket("tok", { factory, baseDelayMs: 100, maxDelayMs: 1000 }));
    act(() => {
      last().open();
      last().send({ mode: "live" });
    });
    act(() => {
      last().drop();
    });
    expect(result.current.connected).toBe(false);
    expect(result.current.status?.mode).toBe("live"); // last known status stays visible
    expect(FakeSocket.all).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(FakeSocket.all).toHaveLength(2); // 1st retry after 100ms
    act(() => {
      last().drop();
    });
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(FakeSocket.all).toHaveLength(2); // 2nd retry waits 200ms
    act(() => {
      vi.advanceTimersByTime(60);
    });
    expect(FakeSocket.all).toHaveLength(3);
    act(() => {
      last().open();
      last().send({ mode: "live" });
    });
    expect(result.current.connected).toBe(true);
    expect(result.current.attempts).toBe(0); // backoff resets after a good message
  });

  it("caps the backoff delay", () => {
    renderHook(() => useStatusSocket("tok", { factory, baseDelayMs: 100, maxDelayMs: 300 }));
    for (let i = 0; i < 5; i++) {
      act(() => {
        last().drop();
      });
      act(() => {
        vi.advanceTimersByTime(300);
      });
    }
    expect(FakeSocket.all).toHaveLength(6);
  });

  it("stops retrying when the server rejects the token (1008)", () => {
    const { result } = renderHook(() => useStatusSocket("bad", { factory, baseDelayMs: 10 }));
    act(() => {
      last().drop(1008);
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(FakeSocket.all).toHaveLength(1);
    expect(result.current.connected).toBe(false);
  });

  it("treats a silent connection as dead and reconnects", () => {
    const { result } = renderHook(() =>
      useStatusSocket("tok", { factory, baseDelayMs: 100, staleAfterMs: 1000 }),
    );
    act(() => {
      last().open();
      last().send({ mode: "paper" });
    });
    act(() => {
      vi.advanceTimersByTime(1600); // no messages: watchdog fires
    });
    expect(result.current.connected).toBe(false);
    expect(FakeSocket.all[0]!.closed).toBe(true);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(FakeSocket.all.length).toBeGreaterThanOrEqual(2);
  });

  it("closes the socket on unmount without reconnecting", () => {
    const { unmount } = renderHook(() => useStatusSocket("tok", { factory, baseDelayMs: 10 }));
    unmount();
    expect(FakeSocket.all[0]!.closed).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("ignores malformed messages", () => {
    const { result } = renderHook(() => useStatusSocket("tok", { factory }));
    act(() => {
      last().onmessage?.({ data: "not json" });
      last().onmessage?.({ data: JSON.stringify({ type: "error", detail: "x" }) });
    });
    expect(result.current.status).toBeNull();
  });
});
