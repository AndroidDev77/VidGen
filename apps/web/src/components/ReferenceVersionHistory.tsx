import { Body1, Caption1, Card, Subtitle2 } from "@fluentui/react-components";
import type { JSX } from "react";

import type { ReferenceVersion } from "../api/references";
import { StatusBadge } from "./StatusBadge";

function identityName(value: unknown): string {
  return typeof value === "string" ? value : "Unknown identity";
}

export function ReferenceVersionHistory({ versions }: { readonly versions: readonly ReferenceVersion[] }): JSX.Element {
  return <div aria-label="Reference version history">
    {versions.map((version) => <Card key={version.id} appearance="outline">
      <Subtitle2>Version {version.version}</Subtitle2>
      <StatusBadge status={version.status} />
      <Body1>{identityName(version.identity.display_name)}</Body1>
      <Caption1>Immutable ID: {version.id}</Caption1>
    </Card>)}
  </div>;
}
