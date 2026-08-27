import { Badge, Tooltip } from "@fluentui/react-components";
import {
  CheckmarkCircleFilled,
  CircleHintHalfVerticalFilled,
  DismissCircleFilled,
  ErrorCircleFilled,
  SubtractCircleFilled,
} from "@fluentui/react-icons";
import type { JSX } from "react";

import { humanize } from "../state/format";

type Tone = "success" | "warning" | "danger" | "informative" | "subtle";

const TONES: Record<string, Tone> = {
  complete: "success",
  render_complete: "success",
  locked: "success",
  approved: "success",
  succeeded: "success",
  running: "informative",
  ingesting: "informative",
  pending: "subtle",
  queued: "subtle",
  stale: "warning",
  partial: "warning",
  failed: "danger",
  cancelled: "danger",
};

/**
 * State is never signalled by colour alone: every badge carries an icon and a
 * word, and the tooltip repeats the raw status for assistive technology.
 */
export function StatusBadge({ status }: { readonly status: string }): JSX.Element {
  const tone = TONES[status] ?? "informative";
  const label = humanize(status);
  return (
    <Tooltip content={`Status: ${label}`} relationship="label">
      <Badge appearance="filled" color={tone} icon={iconFor(tone)} aria-label={`Status: ${label}`}>
        {label}
      </Badge>
    </Tooltip>
  );
}

function iconFor(tone: Tone): JSX.Element {
  switch (tone) {
    case "success":
      return <CheckmarkCircleFilled />;
    case "warning":
      return <ErrorCircleFilled />;
    case "danger":
      return <DismissCircleFilled />;
    case "subtle":
      return <SubtractCircleFilled />;
    default:
      return <CircleHintHalfVerticalFilled />;
  }
}
