import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  CardHeader,
  Caption1,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import { Link } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { listFailures, listProviderAttempts } from "../../api/costs";
import { getCosts, getProjectStatus } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { getProjectVisualQa } from "../../api/visualQa";
import { cancelWorkflow, startWorkflow } from "../../api/workflows";
import { useApiClient } from "../../app/apiContext";
import { CommandsPanel } from "../../components/CommandsPanel";
import { CostSummary } from "../../components/CostSummary";
import { FailurePanel } from "../../components/FailurePanel";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { StageTimeline } from "../../components/StageTimeline";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { ErrorState, LoadingState } from "../../components/states";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
    gap: tokens.spacingHorizontalL,
    marginTop: tokens.spacingVerticalL,
  },
  links: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
});

export function ProjectDashboardPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();
  const { projectId, project, workflow, connectionLabel } = useProjectContext();

  const status = useQuery({
    queryKey: queryKeys.projectStatus(projectId),
    queryFn: ({ signal }) => getProjectStatus(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const costs = useQuery({
    queryKey: queryKeys.costs(projectId),
    queryFn: ({ signal }) => getCosts(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const visualQa = useQuery({
    queryKey: queryKeys.visualQa(projectId),
    queryFn: ({ signal }) => getProjectVisualQa(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const failures = useQuery({
    queryKey: queryKeys.failures(projectId),
    queryFn: ({ signal }) => listFailures(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const attempts = useQuery({
    queryKey: queryKeys.providerAttempts(projectId),
    queryFn: ({ signal }) => listProviderAttempts(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });

  const start = useMutation({
    mutationFn: () => startWorkflow(projectId, newIdempotencyKey("workflow-start"), client),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) }),
  });
  const cancel = useMutation({
    mutationFn: () => cancelWorkflow(projectId, newIdempotencyKey("workflow-cancel"), client),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) }),
  });

  if (project.isPending) {
    return <LoadingState label="Loading the project" rows={3} />;
  }
  if (project.isError) {
    return <ErrorState error={project.error} onRetry={() => void project.refetch()} />;
  }

  const workflowData = workflow.data;
  const started = workflowData !== undefined && workflowData.workflow_id !== null;

  // Every projection is read defensively: a dashboard must not be taken down by
  // one unexpected response shape from a single panel's query.
  const visualQaRuns = visualQa.data?.items ?? [];
  const visualQaSummary = {
    total: visualQaRuns.length,
    passed: visualQaRuns.filter((run) => run.outcome === "PASS").length,
    failed: visualQaRuns.filter((run) => run.outcome === "FAIL").length,
    review: visualQaRuns.filter((run) => run.outcome === "REVIEW").length,
    hardFailures: visualQaRuns.filter((run) => run.hard_failure).length,
  };

  return (
    <div>
      <ProjectStatusHeader
        projectId={projectId}
        projectName={project.data.name}
        pageTitle="Dashboard"
        workflow={workflowData}
        connectionLabel={connectionLabel}
        actions={
          <div className={styles.actions}>
            {!started && (
              <Button
                appearance="primary"
                disabled={start.isPending || status.data?.upload_status !== "completed"}
                onClick={() => start.mutate()}
                data-testid="dashboard-start-workflow"
              >
                {start.isPending ? "Starting…" : "Start the workflow"}
              </Button>
            )}
            {started && !workflowData?.cancelled && (
              <Button
                appearance="secondary"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                {cancel.isPending ? "Cancelling…" : "Cancel the workflow"}
              </Button>
            )}
          </div>
        }
      />

      {start.isError && <ErrorState error={start.error} />}
      {cancel.isError && <ErrorState error={cancel.error} />}

      <Card>
        <CardHeader
          header={<Subtitle2 as="h2">Next step</Subtitle2>}
          description={<Caption1>{nextAction(started, workflowData?.current_stage ?? null)}</Caption1>}
        />
        <div className={styles.links}>
          <Link to={`/projects/${projectId}/transcript`}>Review the transcript</Link>
          <Link to={`/projects/${projectId}/script`}>Review the script</Link>
          <Link to={`/projects/${projectId}/storyboard`}>Review the storyboard</Link>
          <Link to={`/projects/${projectId}/review`}>Final review</Link>
        </div>
      </Card>

      <div className={styles.grid}>
        <Card>
          {workflowData === undefined ? (
            <LoadingState label="Loading workflow status" rows={2} />
          ) : (
            <StageTimeline workflow={workflowData} />
          )}
        </Card>
        <Card>
          {costs.isPending && <LoadingState label="Loading costs" rows={2} />}
          {costs.isError && <ErrorState error={costs.error} />}
          {costs.isSuccess && <CostSummary costs={costs.data} />}
        </Card>
        <Card>
          <CardHeader header={<Subtitle2 as="h2">Visual QA</Subtitle2>} />
          {visualQa.isPending && <LoadingState label="Loading visual QA" rows={2} />}
          {visualQa.isSuccess && (
            <div className={styles.links}>
              <Caption1>
                {visualQaSummary.total === 0
                  ? "No visual-QA result has been recorded yet."
                  : `${visualQaSummary.passed} passed · ${visualQaSummary.failed} blocked · ` +
                    `${visualQaSummary.review} awaiting review`}
              </Caption1>
              {visualQaSummary.hardFailures > 0 && (
                <Caption1>
                  {visualQaSummary.hardFailures} shot
                  {visualQaSummary.hardFailures === 1 ? "" : "s"} carry a hard failure and cannot
                  be rendered.
                </Caption1>
              )}
            </div>
          )}
        </Card>
        <Card>
          <CommandsPanel projectId={projectId} />
        </Card>
        <Card>
          {(failures.isPending || attempts.isPending) && (
            <LoadingState label="Loading failures" rows={2} />
          )}
          {failures.isSuccess && attempts.isSuccess && (
            <FailurePanel failures={failures.data.items} attempts={attempts.data.items} />
          )}
        </Card>
      </div>

      <TechnicalDetails
        entries={[
          ["Project ID", projectId],
          ["Workflow ID", workflowData?.workflow_id ?? null],
          ["Run ID", workflowData?.run_id ?? null],
          ["Source video", status.data?.source_video_id ?? null],
          ["Source asset", status.data?.source_asset_id ?? null],
          ["Upload status", status.data?.upload_status ?? null],
        ]}
      />
    </div>
  );
}

function nextAction(started: boolean, stage: string | null): string {
  if (!started) {
    return "Upload a source video, then start the workflow.";
  }
  switch (stage) {
    case "transcript_acquisition":
      return "The transcript is being acquired. Review it once it appears.";
    case "script_generation":
      return "The recap script is being written. Review and edit it next.";
    case "storyboard":
    case "keyframes":
    case "animation":
    case "shot_orchestration":
      return "Shots are being generated. Inspect the storyboard to follow along.";
    case "rendering":
      return "The final render is in progress.";
    case "review":
      return "The render is ready. Preview it, then approve it.";
    default:
      return "Processing is under way. Progress updates arrive here automatically.";
  }
}
