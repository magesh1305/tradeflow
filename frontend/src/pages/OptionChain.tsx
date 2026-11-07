import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { OptionQuote } from "../api/types";
import { Card, inputClass } from "../components/ui";
import { num, qty } from "../lib/format";

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"];

function Side({ q, reverse }: { q: OptionQuote | null; reverse: boolean }) {
  const cells = [
    <td key="oi" className="px-2 text-right">{q ? qty(q.oi) : "—"}</td>,
    <td key="iv" className="px-2 text-right">{q?.greeks ? num(q.greeks.iv * 100, 1) : "—"}</td>,
    <td key="d" className="px-2 text-right">{q?.greeks ? num(q.greeks.delta, 2) : "—"}</td>,
    <td key="th" className="hidden px-2 text-right lg:table-cell">{q?.greeks ? num(q.greeks.theta, 1) : "—"}</td>,
    <td key="v" className="hidden px-2 text-right lg:table-cell">{q?.greeks ? num(q.greeks.vega, 1) : "—"}</td>,
    <td key="g" className="hidden px-2 text-right xl:table-cell">{q?.greeks ? num(q.greeks.gamma, 5) : "—"}</td>,
    <td key="ba" className="hidden px-2 text-right md:table-cell">{q ? `${num(q.bid)} / ${num(q.ask)}` : "—"}</td>,
    <td key="ltp" className="px-2 text-right font-semibold">{q ? num(q.ltp) : "—"}</td>,
  ];
  return <>{reverse ? cells.reverse() : cells}</>;
}

const heads = ["OI", "IV %", "Δ", "Θ", "Vega", "Γ", "Bid / Ask", "LTP"];
const headClass = ["", "", "", "hidden lg:table-cell", "hidden lg:table-cell", "hidden xl:table-cell", "hidden md:table-cell", ""];

export function OptionChainPage() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState<string>("");
  const [width, setWidth] = useState(10);
  const q = useQuery({
    queryKey: ["chain", underlying, expiry, width],
    queryFn: () => api.optionChain(underlying, expiry || undefined, width),
    refetchInterval: 2000,
  });
  const c = q.data;
  const callHeads = heads.map((h, i) => <th key={`c${h}`} className={`px-2 text-right ${headClass[i] ?? ""}`}>{h}</th>);
  const putHeads = [...heads].reverse().map((h, i) => (
    <th key={`p${h}`} className={`px-2 text-right ${[...headClass].reverse()[i] ?? ""}`}>{h}</th>
  ));
  return (
    <Card
      title={`Option chain${c?.spot ? ` · spot ${num(c.spot)}` : ""}`}
      actions={
        <>
          <select aria-label="Underlying" className={inputClass} value={underlying} onChange={(e) => { setUnderlying(e.target.value); setExpiry(""); }}>
            {UNDERLYINGS.map((u) => <option key={u}>{u}</option>)}
          </select>
          <select aria-label="Expiry" className={inputClass} value={expiry || c?.expiry || ""} onChange={(e) => { setExpiry(e.target.value); }}>
            {(c?.expiries ?? []).map((d) => <option key={d}>{d}</option>)}
          </select>
          <select aria-label="Strikes" className={inputClass} value={width} onChange={(e) => { setWidth(Number(e.target.value)); }}>
            {[5, 10, 15, 20].map((w) => <option key={w} value={w}>±{w}</option>)}
          </select>
        </>
      }
    >
      {q.error && <p className="text-sm text-rose-600">{q.error.message}</p>}
      {c?.note && <p className="mb-2 text-sm text-slate-500">{c.note}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-xs sm:text-sm">
          <thead>
            <tr className="text-slate-500">
              <th colSpan={8} className="py-1 text-center">CALLS</th>
              <th />
              <th colSpan={8} className="py-1 text-center">PUTS</th>
            </tr>
            <tr className="border-b border-slate-200 text-slate-500 dark:border-slate-800">
              {callHeads}
              <th className="px-3 text-center">Strike</th>
              {putHeads}
            </tr>
          </thead>
          <tbody>
            {(c?.rows ?? []).map((r) => (
              <tr
                key={r.strike}
                data-atm={r.is_atm || undefined}
                className={`border-b border-slate-100 dark:border-slate-800/60 ${r.is_atm ? "bg-amber-100 font-semibold dark:bg-amber-900/40" : ""}`}
              >
                <Side q={r.call} reverse={false} />
                <td className="bg-slate-50 px-3 text-center font-semibold dark:bg-slate-800">{num(r.strike, 0)}</td>
                <Side q={r.put} reverse />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
