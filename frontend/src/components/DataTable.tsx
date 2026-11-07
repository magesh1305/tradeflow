import type { ReactNode } from "react";
import { downloadCsv, type CsvColumn } from "../lib/csv";
import { Button, Empty } from "./ui";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  csv?: CsvColumn<T>["value"];
  align?: "left" | "right";
  /** Exported to CSV but not shown in the table. */
  csvOnly?: boolean;
}

export function DataTable<T>({ rows, columns: allColumns, rowKey, empty = "Nothing to show", csvName }: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  empty?: string;
  csvName?: string;
}) {
  const columns = allColumns.filter((c) => !c.csvOnly);
  return (
    <div>
      {csvName && rows.length > 0 && (
        <div className="mb-2 flex justify-end">
          <Button
            onClick={() => {
              downloadCsv(
                csvName,
                rows,
                allColumns.flatMap((c) => (c.csv ? [{ header: c.header, value: c.csv }] : [])),
              );
            }}
          >
            Export CSV
          </Button>
        </div>
      )}
      {rows.length === 0 ? (
        <Empty>{empty}</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs uppercase text-slate-500 dark:border-slate-800">
                {columns.map((c) => (
                  <th key={c.key} className={`whitespace-nowrap px-2 py-2 font-medium ${c.align === "right" ? "text-right" : ""}`}>
                    {c.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={rowKey(r)} className="border-b border-slate-100 last:border-0 dark:border-slate-800/60">
                  {columns.map((c) => (
                    <td key={c.key} className={`whitespace-nowrap px-2 py-1.5 ${c.align === "right" ? "text-right" : ""}`}>
                      {c.render(r)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
