import { Body1, Card, Title3 } from "@fluentui/react-components";
import type { JSX } from "react";

export function ContinuityStatePanel({ bindings }: { readonly bindings: readonly Readonly<Record<string, unknown>>[] }): JSX.Element {
  return <Card><Title3>Shot continuity</Title3><Body1>{bindings.length} immutable shot bundles</Body1>
    <ul>{bindings.map((binding, index) => <li key={String(binding.id ?? index)}>
      Shot {String(binding.storyboard_shot_id ?? "unknown")} — bundle {String(binding.bundle_hash ?? "pending")}
    </li>)}</ul></Card>;
}
