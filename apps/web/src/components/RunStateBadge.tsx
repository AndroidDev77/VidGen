import { Badge, Tooltip } from "@fluentui/react-components";
import {
  CheckmarkCircleFilled,
  DismissCircleFilled,
  ErrorCircleFilled,
  PauseCircleFilled,
  PlayCircleFilled,
  SubtractCircleFilled,
} from "@fluentui/react-icons";
import type { JSX } from "react";
import type { ProjectRunState } from "@vidgen/contracts";

type Tone = "success" | "warning" | "danger" | "informative" | "subtle";

interface Presentation {
  readonly label: string;
  readonly tone: Tone;
  readonly icon: JSX.Element;
  /** What the backend actually observed to report this. */
  readonly explanation: string;
}

/**
 * The whole vocabulary, spelled out. Each entry says what the control plane
 * observed rather than what the pipeline is "probably" doing: `run_state` is
 * read off the recorded execution, so `stopped` means the parent workflow ended
 * short of the last stage — its ordinary way of waiting for a person — and not
 * that something went wrong.
 */
const PRESENTATION: Record<ProjectRunState, Presentation> = {
  not_started: {
    label: "Not started",
    tone: "subtle",
    icon: <SubtractCircleFilled />,
    explanation: "No pipeline run has been started for this project yet.",
  },
  running: {
    label: "Running",
    tone: "informative",
    icon: <PlayCircleFilled />,
    explanation: "The pipeline is running right now.",
  },
  stopped: {
    label: "Stopped",
    tone: "warning",
    icon: <PauseCircleFilled />,
    explanation: "The run ended before the last stage. Nothing is running until you continue it.",
  },
  completed: {
    label: "Completed",
    tone: "success",
    icon: <CheckmarkCircleFilled />,
    explanation: "The run finished and the project reached the end of the pipeline.",
  },
  cancelled: {
    label: "Cancelled",
    tone: "danger",
    icon: <DismissCircleFilled />,
    explanation: "The run was cancelled.",
  },
  failed: {
    label: "Failed",
    tone: "danger",
    icon: <ErrorCircleFilled />,
    explanation: "The run stopped on a failure.",
  },
};

/**
 * Whether a project's pipeline is actually moving.
 *
 * The status badge beside it names the *stage*, which a cancelled, stopped or
 * failed project reports exactly the way one making progress does. Like every
 * other badge here, state is never signalled by colour alone: each carries an
 * icon and a word, and the tooltip says what was observed.
 */
export function RunStateBadge({ state }: { readonly state: ProjectRunState }): JSX.Element {
  // A row from an API that predates `run_state` has no entry to look up.
  const presentation = PRESENTATION[state] ?? PRESENTATION.not_started;
  return (
    <Tooltip content={presentation.explanation} relationship="description" withArrow>
      <Badge
        appearance="tint"
        shape="rounded"
        color={presentation.tone}
        icon={presentation.icon}
        aria-label={`Run: ${presentation.label}. ${presentation.explanation}`}
      >
        {presentation.label}
      </Badge>
    </Tooltip>
  );
}
