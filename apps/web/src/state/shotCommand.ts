import type { ShotCommandProjection, StoryboardShotProjection } from "@vidgen/contracts";

import { humanize } from "./format";

/**
 * What the review UI should say and offer about a shot's control command.
 *
 * A shot's own rows describe the *previous* attempt for the whole window
 * between a person approving or retrying it and the replacement workflow
 * writing something. Rendering only those rows makes an accepted decision look
 * like it never happened, and invites the reviewer to fire it again — which is
 * exactly what this state exists to prevent.
 */
export interface ShotCommandState {
  /** A command is in flight: every action against this shot has to wait. */
  readonly inFlight: boolean;
  /** A short status word for the card's badge, when there is something to say. */
  readonly label: string | null;
  /** A sentence explaining what the UI is showing. Never a raw status code. */
  readonly description: string | null;
  /** The command stopped on a failure the reviewer has to see. */
  readonly failureMessage: string | null;
}

const IDLE: ShotCommandState = {
  inFlight: false,
  label: null,
  description: null,
  failureMessage: null,
};

/** What each shot command type is doing, in the reviewer's words. */
function action(command: ShotCommandProjection): string {
  switch (command.command_type) {
    case "shot_regenerate":
      return "Regenerating";
    case "shot_retry":
      return "Retrying";
    case "shot_review_continue":
      return "Applying your decision";
    default:
      return humanize(command.command_type);
  }
}

export function shotCommandState(
  command: ShotCommandProjection | null | undefined,
): ShotCommandState {
  if (!command) {
    return IDLE;
  }
  if (!command.active) {
    // Terminal and not resolved: the backend only keeps surfacing a command
    // that stopped on a failure, so the action comes back with the reason.
    const reason = command.failure_summary ?? humanize(command.failure_code ?? "unknown_error");
    return {
      inFlight: false,
      label: "Command failed",
      description: null,
      failureMessage: `${action(command)} did not start: ${reason}${
        command.retryable ? " This can be tried again." : ""
      }`,
    };
  }
  if (command.cancel_requested) {
    return {
      inFlight: true,
      label: "Stopping",
      description: `${action(command)} — waiting for the workflow to cancel.`,
      failureMessage: null,
    };
  }
  // Queued and dispatched are genuinely different states to wait in, and a
  // reviewer watching a shot that has not moved deserves to know which it is.
  return command.dispatched
    ? {
        inFlight: true,
        label: "Running",
        description: `${action(command)} — the replacement workflow is running.`,
        failureMessage: null,
      }
    : {
        inFlight: true,
        label: "Queued",
        description: `${action(command)} — queued, waiting for the dispatcher.`,
        failureMessage: null,
      };
}

/** The same state, read straight off a storyboard or inspector shot. */
export function shotCommandStateFor(
  shot: Pick<StoryboardShotProjection, "pending_command">,
): ShotCommandState {
  return shotCommandState(shot.pending_command);
}
