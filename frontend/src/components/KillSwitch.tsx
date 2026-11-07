import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { KillSwitch as KillSwitchState } from "../api/types";
import { useToast } from "../app/toasts";
import { Dialog } from "./Dialog";
import { Button, inputClass } from "./ui";

const PHRASE = "KILL";

/** Big red kill switch. Fires only after the operator types KILL. Usable on a phone. */
export function KillSwitch({ state, compact = false }: { state: KillSwitchState | undefined; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const trip = useMutation({
    mutationFn: () => api.killSwitch("manual (dashboard)"),
    onSuccess: () => {
      toast.push("Kill switch tripped: strategies stopped, squaring off", "success");
      setOpen(false);
      setTyped("");
      void qc.invalidateQueries();
    },
  });
  const reset = useMutation({
    mutationFn: api.resetKillSwitch,
    onSuccess: () => {
      toast.push("Kill switch reset. Strategies stay stopped until you start them.", "info");
      void qc.invalidateQueries();
    },
  });

  if (state?.tripped) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-red-700 px-2 py-1 text-sm font-bold text-white" title={state.reason}>
          KILL SWITCH ACTIVE
        </span>
        {!compact && (
          <Button onClick={() => reset.mutate()} disabled={reset.isPending}>
            Reset
          </Button>
        )}
      </div>
    );
  }

  return (
    <>
      <Button
        variant="danger"
        className={compact ? "min-h-11 w-full text-base font-bold" : "min-h-11 px-5 text-base font-bold"}
        onClick={() => {
          setOpen(true);
        }}
      >
        KILL SWITCH
      </Button>
      <Dialog
        open={open}
        title="Trip the kill switch?"
        onClose={() => {
          setOpen(false);
          setTyped("");
        }}
      >
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
          Stops every strategy, cancels open orders and squares off all positions (MIS and NRML).
          Type <strong>{PHRASE}</strong> to confirm.
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
            <Button
              className="flex-1"
              onClick={() => {
                setOpen(false);
                setTyped("");
              }}
            >
              Cancel
            </Button>
            <Button type="submit" variant="danger" className="flex-1 font-bold" disabled={typed !== PHRASE || trip.isPending}>
              {trip.isPending ? "Tripping…" : "Trip kill switch"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
