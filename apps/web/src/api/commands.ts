import type {
  ControlCommand,
  ControlCommandCollectionResponse,
  ControlCommandResponse,
  WorkflowContinuationRequest,
} from "@vidgen/contracts";
import { isTerminalCommandStatus } from "@vidgen/contracts";

import { apiClient, newIdempotencyKey, type ApiResponse, type VidGenClient } from "./client";

/**
 * The T18b control plane, as the browser sees it.
 *
 * Every asynchronous action in the UI resolves to a command here. The panel
 * that started it polls this endpoint rather than assuming an HTTP 202 meant
 * the work is running: a command is only `running` once the backend recorded
 * the workflow it actually started.
 */
export function listCommands(
  projectId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ControlCommandCollectionResponse>> {
  return client.get<ControlCommandCollectionResponse>(
    `/api/v1/projects/${projectId}/commands`,
    signal ? { signal } : {},
  );
}

export function getCommand(
  projectId: string,
  commandId: string,
  client: VidGenClient = apiClient,
  signal?: AbortSignal,
): Promise<ApiResponse<ControlCommandResponse>> {
  return client.get<ControlCommandResponse>(
    `/api/v1/projects/${projectId}/commands/${commandId}`,
    signal ? { signal } : {},
  );
}

export function cancelCommand(
  projectId: string,
  commandId: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ControlCommandResponse>> {
  return client.post<ControlCommandResponse>(
    `/api/v1/projects/${projectId}/commands/${commandId}:cancel`,
    {},
  );
}

export function retryCommand(
  projectId: string,
  commandId: string,
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ControlCommandResponse>> {
  return client.post<ControlCommandResponse>(
    `/api/v1/projects/${projectId}/commands/${commandId}:retry`,
    {},
  );
}

export function continueWorkflow(
  projectId: string,
  body: WorkflowContinuationRequest,
  idempotencyKey: string = newIdempotencyKey("workflow-continue"),
  client: VidGenClient = apiClient,
): Promise<ApiResponse<ControlCommandResponse>> {
  return client.post<ControlCommandResponse>(`/api/v1/projects/${projectId}/workflow:continue`, {
    body,
    idempotencyKey,
  });
}

/**
 * How often to poll a command, or `false` once it has reached a terminal state.
 *
 * React Query accepts `false` to mean "stop", which is exactly what the UI
 * must do: a completed, failed or cancelled command never changes again, and
 * continuing to poll it would be a request per second for no new information.
 */
export function commandPollInterval(command: ControlCommand | undefined): number | false {
  if (command === undefined) {
    return 2_000;
  }
  return isTerminalCommandStatus(command.status) ? false : 2_000;
}

/** The short, human sentence a panel shows beside a command's status chip. */
export function describeCommand(command: ControlCommand): string {
  switch (command.status) {
    case "pending":
      return "Queued. A worker will pick this up shortly.";
    case "claimed":
    case "dispatching":
      return "Starting the workflow…";
    case "running":
      return command.progress.phase === ""
        ? "Running."
        : `Running: ${command.progress.phase}.`;
    case "awaiting_review":
      return command.progress.waiting_reason === ""
        ? "Waiting for a decision."
        : `Waiting: ${command.progress.waiting_reason.replaceAll("_", " ")}.`;
    case "completed":
      return "Completed.";
    case "failed":
      return command.failure?.summary ?? "This command failed.";
    case "cancelled":
      return "Cancelled.";
    case "superseded":
      return "Replaced by a newer command.";
  }
}
