// TradingView Lightweight Charts (v5): equity curve (line) and daily P&L (histogram).

import {
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

interface Point {
  time: string; // YYYY-MM-DD
  value: number;
}

function useChart(build: (chart: IChartApi) => void, deps: unknown[]) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const dark = document.documentElement.classList.contains("dark");
    const chart = createChart(el.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: dark ? "#cbd5e1" : "#334155",
      },
      grid: {
        vertLines: { color: dark ? "#1e293b" : "#e2e8f0" },
        horzLines: { color: dark ? "#1e293b" : "#e2e8f0" },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
    });
    build(chart);
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return el;
}

export function EquityChart({ points }: { points: Point[] }) {
  const el = useChart((chart) => {
    const series = chart.addSeries(LineSeries, { color: "#0284c7", lineWidth: 2 });
    series.setData(points.map((p) => ({ time: p.time, value: p.value })));
  }, [points]);
  return <div ref={el} className="h-72 w-full" data-testid="equity-chart" />;
}

export function PnlBars({ points }: { points: Point[] }) {
  const el = useChart((chart) => {
    const series = chart.addSeries(HistogramSeries, {});
    series.setData(
      points.map((p) => ({
        time: p.time,
        value: p.value,
        color: p.value >= 0 ? "#059669" : "#e11d48",
      })),
    );
  }, [points]);
  return <div ref={el} className="h-72 w-full" data-testid="pnl-bars" />;
}
