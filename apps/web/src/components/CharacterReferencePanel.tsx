import { Card, Title3 } from "@fluentui/react-components";
import type { JSX } from "react";
import type { ReferenceVersion } from "../api/references";
import { ReferenceVersionHistory } from "./ReferenceVersionHistory";

export function CharacterReferencePanel({ versions }: { readonly versions: readonly ReferenceVersion[] }): JSX.Element {
  return <Card><Title3>Character references</Title3><ReferenceVersionHistory versions={versions} /></Card>;
}
