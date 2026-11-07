// The admin token lives in memory and sessionStorage only (cleared when the tab
// closes). Never localStorage: it would survive on shared or lost devices.

const KEY = "tradeflow.adminToken";
let memory: string | null = null;
const listeners = new Set<() => void>();

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // privacy mode or storage disabled: memory only
  }
}

export function getToken(): string | null {
  if (memory === null) {
    memory = storage()?.getItem(KEY) ?? null;
  }
  return memory;
}

export function setToken(token: string | null): void {
  memory = token;
  const s = storage();
  if (token === null) s?.removeItem(KEY);
  else s?.setItem(KEY, token);
  listeners.forEach((l) => {
    l();
  });
}

export function onTokenChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
