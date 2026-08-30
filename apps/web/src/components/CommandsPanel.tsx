import {
  Body1,
  Caption1,
  CardHeader,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import { isTerminalCommandStatus } from "@vidgen/contracts";
import type { JSX } from "react";

import { describeCommand, listCommands } from "../api/commands";
import { queryKeys } from "../api/queryKeys";
import { useApiClient } from "../app/apiContext";
import { StatusBadge } from "./StatusBadge";
import { ErrorState, LoadingState } from "./states";

const useStyles = makeStyles({
  list: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  row: {
    display: "flex",
    alignItems: "baseline",
    gap: tokens.spacingHorizontalS,
    flexWrap: "wrap",
  },
});

/** Human labels for the command types, so the panel is not a list of slugs. */
const LABELS: Record<string, string> = {
  reference_build: "Build continuity references",
  reference_generate: "Regenerate a reference sheet",
  reference_apply: "Apply approved references",
  shot_regenerate: "Regenerate a shot",
  shot_retry: "Retry a shot",
  shot_review_continue: "Continue a reviewed shot",
  transcript_revision: "Rebuild after a transcript edit",
  script_revision: "Rebuild after a script revision",
  final_qa_run: "Run final QA",
  final_qa_remediation: "Remediate a final-QA finding",
  render_rerender: "Re-render",
  project_continue: "Continue the project",
};

export interface CommandsPanelProps {
  readonly projectId: string;
}

/**
 * Every asynchronous command this project has issued, and what became of it.
 *
 * This is the panel that makes the control plane visible: a queued command
 * appears here the moment the API accepts it, moves to running only when a
 * workflow was actually started, and ends in a state that names either its
 * result or its failure. Polling stops once nothing here can still change.
 */
export function CommandsPanel({ projectId }: CommandsPanelProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();

  const commands = useQuery({
    queryKey: queryKeys.commands(projectId),
    queryFn: ({ signal }) =>
      listCommands(projectId, client, signal).then((response) => response.data),
    enabled: projectId !== "",
    refetchInterval: ({ state }) => {
      const pending = Array.isArray(state.data?.items) ? state.data.items : [];
      const active = pending.some((item) => !isTerminalCommandStatus(item.status));
      return active ? 3_000 : false;
    },
  });

  if (commands.isPending) {
    return <LoadingState label="Loading commands" rows={2} />;
  }
  if (commands.isError) {
    return <ErrorState error={commands.error} onRetry={() => void commands.refetch()} />;
  }

  // Read defensively. A dashboard must not be taken down by one unexpected
  // response shape from a single panel's query - the other panels here follow
  // the same rule.
  const items = Array.isArray(commands.data?.items) ? commands.data.items : [];
  const runs = Array.isArray(commands.data?.generation_runs)
    ? commands.data.generation_runs
    : [];
  const active = runs.find((run) => run.status === "active" || run.status === "awaiting_review");

  return (
    <div>
      <CardHeader
        header={<Subtitle2 as="h2">Commands</Subtitle2>}
        description={
          <Caption1>
            {active === undefined
              ? "No generation run is active."
              : `Generation run ${active.sequence}, from ${String(
                  active.entry_stage ?? "",
                ).replaceAll("_", " ")}.`}
          </Caption1>
        }
      />
      {items.length === 0 ? (
        <Body1>No command has been issued for this project yet.</Body1>
      ) : (
        <ul className={styles.list} aria-label="Control commands">
          {items.map((command) => (
            <li key={command.command_id} className={styles.row}>
              <StatusBadge status={command.status} />
              <Body1>{LABELS[command.command_type] ?? command.command_type}</Body1>
              <Caption1>{describeCommand(command)}</Caption1>
              {command.workflow_id !== null && (
                <Caption1>{`Workflow ${command.workflow_id}`}</Caption1>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
