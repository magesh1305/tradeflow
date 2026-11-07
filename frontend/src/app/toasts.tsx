import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type ToastKind = "error" | "success" | "info";
interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastApi {
  push: (text: string, kind?: ToastKind) => void;
}

const Ctx = createContext<ToastApi | null>(null);
let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);
  const push = useCallback(
    (text: string, kind: ToastKind = "info") => {
      const id = nextId++;
      setToasts((ts) => [...ts.slice(-4), { id, kind, text }]);
      setTimeout(() => {
        dismiss(id);
      }, kind === "error" ? 8000 : 4000);
    },
    [dismiss],
  );
  const api = useMemo(() => ({ push }), [push]);
  const colours: Record<ToastKind, string> = {
    error: "bg-rose-600 text-white",
    success: "bg-emerald-600 text-white",
    info: "bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900",
  };
  return (
    <Ctx.Provider value={api}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed inset-x-0 top-2 z-50 flex flex-col items-center gap-2 px-2"
      >
        {toasts.map((t) => (
          <button
            key={t.id}
            type="button"
            role={t.kind === "error" ? "alert" : "status"}
            onClick={() => {
              dismiss(t.id);
            }}
            className={`pointer-events-auto max-w-lg rounded-md px-4 py-2 text-left text-sm shadow-lg ${colours[t.kind]}`}
          >
            {t.text}
          </button>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useToast outside ToastProvider");
  return ctx;
}
