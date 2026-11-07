// After a restart, if our records and the broker's disagree, robots are blocked
// until a person checks. This lets them lift the block after typing RESOLVE.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useToast } from "../app/toasts";
import { Dialog } from "./Dialog";
import { Button, inputClass } from "./ui";

export function ResolveBlock() {
  const [open, setOpen] = useState(false);
  const [adopt, setAdopt] = useState(false);
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const qc = useQueryClient();
  const close = () => {
    setOpen(false);
    setTyped("");
  };
  const m = useMutation({
    mutationFn: () => api.resolveRecovery(adopt),
    onSuccess: () => {
      toast.push("Check complete. Robots can start again.", "success");
      close();
      void qc.invalidateQueries();
    },
  });
  return (
    <>
      <Button variant="danger" onClick={() => { setOpen(true); }}>I&apos;ve checked, continue</Button>
      <Dialog open={open} title="Continue after the check?" onClose={close}>
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
          First open Zerodha Kite and look at your positions and orders. Then choose what is true:
        </p>
        <div className="mb-3 space-y-2 text-sm">
          <label className="flex gap-2 rounded-xl border border-slate-200 p-3 dark:border-slate-700">
            <input type="radio" name="adopt" checked={!adopt} onChange={() => { setAdopt(false); }} />
            <span>I fixed it in Kite myself (closed or matched the trades). Just continue.</span>
          </label>
          <label className="flex gap-2 rounded-xl border border-slate-200 p-3 dark:border-slate-700">
            <input type="radio" name="adopt" checked={adopt} onChange={() => { setAdopt(true); }} />
            <span>Take over the trades Kite shows, so I can close them from here.</span>
          </label>
        </div>
        <input
          aria-label="Type RESOLVE to confirm"
          placeholder="Type RESOLVE"
          className={`${inputClass} mb-3 text-center font-bold tracking-widest`}
          value={typed}
          autoComplete="off"
          onChange={(e) => { setTyped(e.target.value.toUpperCase()); }}
        />
        <div className="flex gap-2">
          <Button className="flex-1" onClick={close}>Cancel</Button>
          <Button variant="primary" className="flex-1" disabled={typed !== "RESOLVE" || m.isPending} onClick={() => m.mutate()}>
            Continue
          </Button>
        </div>
      </Dialog>
    </>
  );
}
