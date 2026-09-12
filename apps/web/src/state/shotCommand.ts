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
  /**
   * The command is working, so every action that would enqueue another one
   * waits. A command parked on a human decision is deliberately *not* in
   * flight by this definition: the decision it waits for is the one the UI
   * would otherwise disable, which would leave the shot with no way forward.
   */
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
  if (command.active && command.awaiting_review) {
    // Parked on a person. Say so, and leave every decision available.
    return {
      inFlight: false,
      label: "Waiting on you",
      description: `${action(command)} — the shot is waiting on your decision.`,
      failureMessage: null,
    };
  }
  if (!command.active) {
    // Terminal and not resolved: the backend only keeps surfacing a command
    // that stopped on a failure, so the action comes back with the reason.
    const reason = command.failure_summary ?? humanize(command.failure_code ?? "unknown_error");
    // A command can fail before it ever reached a worker or after its
    // replacement workflow ran and produced nothing; saying "did not start"
    // for the second is simply untrue, and the paid attempt it implies away
    // is the part a reviewer most needs to know about.
    const stage = command.dispatched ? "did not finish" : "did not start";
    return {
      inFlight: false,
      label: "Command failed",
      description: null,
      failureMessage: `${action(command)} ${stage}: ${reason}${
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
