// Submitting today's plan, shared by the Plan page (simple and advanced) and the
// Start here guide. Loosening limits during market hours asks for a typed LOOSEN.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError, api } from "../api/client";
import type { DailyPlan, PlanResult, SettingsInfo } from "../api/types";
import { useToast } from "../app/toasts";
import { Dialog } from "./Dialog";
import { Button, inputClass } from "./ui";

export function usePlanSubmit(info: SettingsInfo, onDone?: (r: PlanResult) => void): {
  submit: (plan: DailyPlan) => void;
  pending: boolean;
  dialog: ReactNode;
} {
  const [loosen, setLoosen] = useState<{ plan: DailyPlan; items: string[] } | null>(null);
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const title = (id: string) => info.strategies.find((s) => s.id === id)?.title || id;
  const m = useMutation({
    mutationFn: ({ plan, confirm }: { plan: DailyPlan; confirm?: string }) => api.submitPlan(plan, confirm),
    meta: { handlesErrors: true },
    onSuccess: (r) => {
      setLoosen(null);
      setTyped("");
      toast.push(
        [info.submitted ? "Plan updated" : "Today's plan submitted",
          r.started.length ? `started: ${r.started.map(title).join(", ")}` : "",
          r.stopped.length ? `stopped: ${r.stopped.map(title).join(", ")}` : ""].filter(Boolean).join(" · "),
        "success",
      );
      for (const w of r.warnings) toast.push(w, "error");
      void qc.invalidateQueries();
      onDone?.(r);
    },
    onError: (e, vars) => {
      if (e instanceof ApiError && e.status === 409 && typeof e.detail === "object" && e.detail !== null
        && "loosened" in e.detail) {
        setLoosen({ plan: vars.plan, items: (e.detail as { loosened: string[] }).loosened });
        return;
      }
      toast.push(e.message, "error");
    },
  });
  const close = () => {
    setLoosen(null);
    setTyped("");
  };
  const dialog = (
    <Dialog open={loosen !== null} title="Loosen limits during market hours?" onClose={close}>
      <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">The market is open, and these limits become less strict (more risk):</p>
      <ul className="mb-3 list-disc pl-5 text-sm">
        {loosen?.items.map((i) => <li key={i}>{i}</li>)}
      </ul>
      <p className="mb-2 text-sm">
        This change is recorded and an alert is sent. Type <strong>{info.loosen_confirmation}</strong> to confirm.
      </p>
      <input
        aria-label="Type LOOSEN to confirm"
        className={`${inputClass} mb-3 text-center font-bold tracking-widest`}
        value={typed}
        autoComplete="off"
        onChange={(e) => {
          setTyped(e.target.value.toUpperCase());
        }}
      />
      <div className="flex gap-2">
        <Button className="flex-1" onClick={close}>Cancel</Button>
        <Button
          variant="danger"
          className="flex-1"
          disabled={typed !== info.loosen_confirmation || m.isPending}
          onClick={() => {
            if (loosen) m.mutate({ plan: loosen.plan, confirm: info.loosen_confirmation });
          }}
        >
          Loosen limits
        </Button>
      </div>
    </Dialog>
  );
  return { submit: (plan) => { m.mutate({ plan }); }, pending: m.isPending, dialog };
}
