import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { KillSwitch as KillSwitchState } from "../api/types";
import { useToast } from "../app/toasts";
import { Dialog } from "./Dialog";
import { Icon } from "./Icon";
import { Button, inputClass } from "./ui";

const PHRASE = "KILL";

/**
 * Emergency stop (the kill switch): stops every robot, cancels waiting orders and
 * closes all trades. Fires only after typing KILL, so it can't be pressed by accident.
 */
export function KillSwitch({ state, compact = false }: { state: KillSwitchState | undefined; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const close = () => {
    setOpen(false);
    setTyped("");
  };
  const trip = useMutation({
    mutationFn: () => api.killSwitch("manual (dashboard)"),
    onSuccess: () => {
      toast.push("Emergency stop done: robots stopped, all trades are being closed", "success");
      close();
      void qc.invalidateQueries();
    },
  });
  const reset = useMutation({
    mutationFn: api.resetKillSwitch,
    onSuccess: () => {
      toast.push("Emergency stop cleared. Robots stay off until you start them.", "info");
      void qc.invalidateQueries();
    },
  });

  if (state?.tripped) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="inline-flex items-center gap-1.5 rounded-full bg-red-700 px-3 py-1 text-xs font-bold text-white"
          title={state.reason}
        >
          <Icon name="stop" className="h-4 w-4" /> EMERGENCY STOP ON
        </span>
        {!compact && (
          <Button onClick={() => reset.mutate()} disabled={reset.isPending}>
            Clear
          </Button>
        )}
      </div>
    );
  }

  return (
    <>
      <Button
        variant="danger"
        className={compact ? "min-h-9 px-3 text-xs font-bold" : "min-h-10 px-4 font-bold"}
        onClick={() => {
          setOpen(true);
        }}
      >
        <Icon name="stop" className="h-4 w-4" />
        {compact ? "STOP ALL" : "EMERGENCY STOP"}
      </Button>
      <Dialog open={open} title="Stop everything now?" onClose={close}>
        <ul className="mb-3 space-y-1.5 text-sm text-slate-700 dark:text-slate-300">
          <li className="flex gap-2"><Icon name="check" className="h-4 w-4 shrink-0 text-red-600" /> Every robot stops.</li>
          <li className="flex gap-2"><Icon name="check" className="h-4 w-4 shrink-0 text-red-600" /> Orders waiting to fill are cancelled.</li>
          <li className="flex gap-2"><Icon name="check" className="h-4 w-4 shrink-0 text-red-600" /> All open trades are closed at the market price, including overnight ones.</li>
        </ul>
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-400">
          Type <strong className="text-slate-900 dark:text-white">{PHRASE}</strong> below to confirm.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (typed === PHRASE) trip.mutate();
          }}
        >
          <input
            aria-label="Type KILL to confirm"
            className={`${inputClass} mb-3 text-center text-lg font-bold tracking-widest`}
            value={typed}
            autoCapitalize="characters"
            autoComplete="off"
            onChange={(e) => {
              setTyped(e.target.value.toUpperCase());
            }}
          />
          <div className="flex gap-2">
            <Button className="flex-1" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" className="flex-1 font-bold" disabled={typed !== PHRASE || trip.isPending}>
              {trip.isPending ? "Stopping…" : "Stop everything"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
