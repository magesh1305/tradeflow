// Form generated from a strategy's pydantic JSON schema. The backend validates
// again; its 422 messages come back as toasts.

import { useState } from "react";
import type { JsonSchema, JsonSchemaProperty } from "../api/types";
import { Button, inputClass } from "./ui";

type Values = Record<string, string | boolean>;

function toForm(params: Record<string, unknown>): Values {
  const out: Values = {};
  for (const [k, v] of Object.entries(params)) {
    out[k] =
      typeof v === "boolean"
        ? v
        : v === null || v === undefined
          ? ""
          : typeof v === "string" || typeof v === "number"
            ? String(v)
            : JSON.stringify(v);
  }
  return out;
}

export function coerce(schema: JsonSchema, values: Values): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, prop] of Object.entries(schema.properties ?? {})) {
    const v = values[k];
    if (v === undefined) continue;
    if (prop.type === "boolean") out[k] = Boolean(v);
    else if (prop.type === "integer") out[k] = v === "" ? null : Number.parseInt(String(v), 10);
    else if (prop.type === "number") out[k] = v === "" ? null : Number(v);
    else out[k] = v;
  }
  return out;
}

function inputType(p: JsonSchemaProperty): string {
  if (p.type === "integer" || p.type === "number") return "number";
  if (p.format === "time") return "time";
  if (p.format === "date") return "date";
  return "text";
}

export function ParamEditor({ schema, params, disabled, onSave, saving }: {
  schema: JsonSchema;
  params: Record<string, unknown>;
  disabled: boolean;
  saving: boolean;
  onSave: (params: Record<string, unknown>) => void;
}) {
  // The parent remounts this form (via `key`) when the saved params change.
  const [values, setValues] = useState<Values>(() => toForm(params));
  const props = Object.entries(schema.properties ?? {});
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave(coerce(schema, values));
      }}
    >
      <fieldset disabled={disabled} className="grid gap-3 sm:grid-cols-2">
        {props.map(([name, p]) => (
          <label key={name} className="text-sm">
            <span className="mb-1 block text-slate-600 dark:text-slate-300">{p.title ?? name}</span>
            {p.type === "boolean" ? (
              <input
                type="checkbox"
                checked={Boolean(values[name])}
                onChange={(e) => {
                  setValues({ ...values, [name]: e.target.checked });
                }}
              />
            ) : (
              <input
                className={inputClass}
                type={inputType(p)}
                step={p.type === "number" ? "any" : p.format === "time" ? 60 : undefined}
                min={p.minimum ?? p.exclusiveMinimum}
                max={p.maximum ?? p.exclusiveMaximum}
                value={String(values[name] ?? "")}
                onChange={(e) => {
                  setValues({ ...values, [name]: e.target.value });
                }}
              />
            )}
          </label>
        ))}
      </fieldset>
      <div className="mt-3 flex items-center gap-3">
        <Button type="submit" variant="primary" disabled={disabled || saving}>
          {saving ? "Saving…" : "Save params"}
        </Button>
        {disabled && <span className="text-xs text-slate-500">Stop the strategy to edit its params.</span>}
      </div>
    </form>
  );
}
