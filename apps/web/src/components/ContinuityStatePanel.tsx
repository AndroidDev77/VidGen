import { Body1, Card, Title3 } from "@fluentui/react-components";
import type { JSX } from "react";

function text(value: unknown, fallback: string): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

export function ContinuityStatePanel({ bindings }: { readonly bindings: readonly Readonly<Record<string, unknown>>[] }): JSX.Element {
  return <Card><Title3>Shot continuity</Title3><Body1>{bindings.length} immutable shot bundles</Body1>
    <ul>{bindings.map((binding, index) => <li key={text(binding.id, String(index))}>
      Shot {text(binding.storyboard_shot_id, "unknown")} — bundle {text(binding.bundle_hash, "pending")}
    </li>)}</ul></Card>;
}
