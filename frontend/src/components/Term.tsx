// A trading word you can tap to read what it means (dotted underline + popover).

import { useEffect, useRef, useState, type ReactNode } from "react";
import { GLOSSARY, type GlossaryEntry, type GlossaryKey } from "../lib/glossary";

export function Term({ k, children }: { k: GlossaryKey; children?: ReactNode }) {
  const entry: GlossaryEntry = GLOSSARY[k];
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  return (
    <span ref={ref} className="relative inline-block">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
        }}
        className="cursor-help underline decoration-slate-400 decoration-dotted underline-offset-4 hover:decoration-brand-500"
      >
        {children ?? entry.term}
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-0 top-full z-40 mt-2 block w-72 max-w-[80vw] rounded-xl border border-slate-200 bg-white p-3 text-left text-sm font-normal normal-case tracking-normal text-slate-700 shadow-xl dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        >
          <span className="mb-1 block font-semibold text-slate-900 dark:text-white">{entry.term}</span>
          {entry.short}
          {entry.long && <span className="mt-1 block text-slate-500 dark:text-slate-400">{entry.long}</span>}
        </span>
      )}
    </span>
  );
}
