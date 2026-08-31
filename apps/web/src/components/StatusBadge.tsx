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
  // T17b render-job states. A render in flight reads as informative, not as a
  // silent "pending", so the dashboard shows what is actually happening.
  render_queued: "subtle",
  render_claiming: "informative",
  render_preparing: "informative",
  render_manifest_ready: "informative",
  render_rendering: "informative",
  render_verifying: "informative",
  render_persisting: "informative",
  render_failed: "danger",
  render_cancelled: "danger",
  ingesting: "informative",
  pending: "subtle",
  queued: "subtle",
  // T18b control-command statuses. "awaiting_review" is a warning rather than
  // an error: nothing is wrong, but nothing progresses until a person decides.
  claimed: "informative",
  dispatching: "informative",
  awaiting_review: "warning",
  completed: "success",
  superseded: "subtle",
  stale: "warning",
  partial: "warning",
  failed: "danger",
  cancelled: "danger",
};

/**
 * State is never signalled by colour alone: every badge carries an icon and a
 * word, and the tooltip repeats the raw status for assistive technology.
 *
 * The tint appearance is deliberate. A table or a stage grid shows a dozen of
 * these at once, and a dozen saturated fills turn the page into a traffic
 * light; a tinted chip keeps the same colour coding legible while letting the
 * content stay the loudest thing on screen.
 */
const LABELS: Record<string, string> = {
  episode_analysis_failed: "Analysis failed",
  transcript_acquisition_failed: "Transcript failed",
  transcription_failed: "Transcript failed",
  script_generation_failed: "Script failed",
  storyboard_failed: "Storyboard failed",
  shot_generation_failed: "Shot gen failed",
  narration_failed: "Narration failed",
  rendering_failed: "Render failed",
  awaiting_upload: "Awaiting upload",
};

function inferTone(status: string): Tone {
  if (status in TONES) return TONES[status]!;
  if (status.includes("failed") || status.includes("error")) return "danger";
  if (status.includes("cancelled")) return "danger";
  if (status.includes("complete") || status.includes("succeeded")) return "success";
  if (status.includes("running") || status.includes("ing")) return "informative";
  return "subtle";
}

export function StatusBadge({ status }: { readonly status: string }): JSX.Element {
  const tone = inferTone(status);
  const label = LABELS[status] ?? humanize(status);
  return (
    <Tooltip content={`Status: ${label}`} relationship="label">
      <Badge
        appearance="tint"
        shape="rounded"
        color={tone}
        icon={iconFor(tone)}
        aria-label={`Status: ${label}`}
      >
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
