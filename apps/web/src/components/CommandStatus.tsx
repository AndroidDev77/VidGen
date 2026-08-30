import {
  Body1,
  Button,
  Caption1,
  ProgressBar,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { JSX } from "react";

import {
  cancelCommand,
  commandPollInterval,
  describeCommand,
  getCommand,
  retryCommand,
} from "../api/commands";
import { queryKeys } from "../api/queryKeys";
import { useApiClient } from "../app/apiContext";
import { StatusBadge } from "./StatusBadge";

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXS,
    padding: tokens.spacingVerticalS,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground2,
  },
  row: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    flexWrap: "wrap",
  },
});

export interface CommandStatusProps {
  readonly projectId: string;
  readonly commandId: string;
  /** A short description of what this command was asked to do. */
  readonly label: string;
}

/**
 * Live status for one durable control command.
 *
 * This component is the reason the UI never has to claim work started from an
 * HTTP 202. It shows exactly what the backend recorded - queued, dispatched,
 * running, waiting on a person, or failed with an actionable code - and it
 * stops polling the moment the command reaches a terminal state.
 */
export function CommandStatus({ projectId, commandId, label }: CommandStatusProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: queryKeys.command(projectId, commandId),
    queryFn: ({ signal }) =>
      getCommand(projectId, commandId, client, signal).then((response) => response.data.command),
    refetchInterval: ({ state }) => commandPollInterval(state.data),
  });

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.command(projectId, commandId) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.commands(projectId) });
  };
  const cancel = useMutation({
    mutationFn: () => cancelCommand(projectId, commandId, client),
    onSuccess: invalidate,
  });
  const retry = useMutation({
    mutationFn: () => retryCommand(projectId, commandId, client),
    onSuccess: invalidate,
  });

  const command = query.data;
  if (command === undefined) {
    return (
      <div className={styles.root} aria-busy="true">
        <Caption1>{label}</Caption1>
        <Body1>Loading the command…</Body1>
      </div>
    );
  }

  const busy =
    command.status === "running" ||
    command.status === "claimed" ||
    command.status === "dispatching";

  return (
    <div className={styles.root} data-testid={`command-${commandId}`}>
      <Caption1>{label}</Caption1>
      <div className={styles.row}>
        <StatusBadge status={command.status} />
        <Body1>{describeCommand(command)}</Body1>
      </div>
      {busy && (
        <ProgressBar
          value={command.progress.percent / 100}
          aria-label={`${label} progress`}
        />
      )}
      {command.workflow_id !== null && (
        <Caption1>Workflow {command.workflow_id}</Caption1>
      )}
      {command.cancel_requested && (
        <Caption1>Stopping — waiting for the workflow to cancel.</Caption1>
      )}
      {command.failure !== null && (
        <Caption1 role="alert">
          {command.failure.code}
          {command.failure.retryable ? " — this can be retried." : " — this needs a change first."}
        </Caption1>
      )}
      <div className={styles.row}>
        {command.permitted_actions.includes("cancel") && (
          <Button size="small" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
            Cancel
          </Button>
        )}
        {command.permitted_actions.includes("retry") && (
          <Button size="small" onClick={() => retry.mutate()} disabled={retry.isPending}>
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}
