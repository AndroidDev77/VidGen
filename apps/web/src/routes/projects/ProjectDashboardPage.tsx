import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Body1,
  Button,
  Caption1,
  makeStyles,
  shorthands,
  tokens,
} from "@fluentui/react-components";
import {
  ArrowRightRegular,
  DismissCircleRegular,
  EyeRegular,
  LightbulbFilamentRegular,
  PlayRegular,
} from "@fluentui/react-icons";
import type { JSX } from "react";
import { Link, useNavigate } from "react-router-dom";

import { newIdempotencyKey } from "../../api/client";
import { listFailures, listProviderAttempts } from "../../api/costs";
import { deleteProject, getCosts, getProjectStatus } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { getProjectVisualQa } from "../../api/visualQa";
import { cancelWorkflow, startWorkflow } from "../../api/workflows";
import { useApiClient } from "../../app/apiContext";
import { CommandsPanel } from "../../components/CommandsPanel";
import { CostSummary } from "../../components/CostSummary";
import { FailurePanel } from "../../components/FailurePanel";
import { ProjectStatusHeader } from "../../components/ProjectStatusHeader";
import { StageTimeline } from "../../components/StageTimeline";
import { SectionCard, StatTile, StatTiles } from "../../components/Surface";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { ErrorState, LoadingState } from "../../components/states";
import { formatDurationSeconds, formatMoney, formatStage } from "../../state/format";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({
  stack: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalL },
  // Two columns on a desk, one below that. `auto-fit` would fit three tracks at
  // 1440px and leave a permanently empty one, since the panels here are an odd
  // number; pinning the count keeps the page balanced at every width.
  grid: {
    display: "grid",
    gridTemplateColumns: "minmax(0, 1fr)",
    gap: tokens.spacingHorizontalL,
    alignItems: "start",
    "@media (min-width: 900px)": {
      gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
    },
  },
  wide: { gridColumn: "1 / -1" },

  // The next step is the one thing a returning owner is looking for, so it gets
  // a brand-tinted band rather than another neutral card in the stack.
  nextStep: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalL,
    flexWrap: "wrap",
    padding: `${tokens.spacingVerticalL} ${tokens.spacingHorizontalL}`,
    borderRadius: tokens.borderRadiusLarge,
    border: `1px solid ${tokens.colorBrandStroke2}`,
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorNeutralForeground1,
  },
  nextStepIcon: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    width: "40px",
    height: "40px",
    borderRadius: tokens.borderRadiusCircular,
    backgroundColor: tokens.colorNeutralBackground1,
    color: tokens.colorBrandForeground2,
    fontSize: tokens.fontSizeBase600,
  },
  nextStepText: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: "220px" },
  nextStepLabel: {
    color: tokens.colorBrandForeground2,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    fontWeight: tokens.fontWeightSemibold,
  },

  links: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  quickLink: {
    display: "inline-flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXS,
    padding: `${tokens.spacingVerticalXS} ${tokens.spacingHorizontalM}`,
    borderRadius: tokens.borderRadiusCircular,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    color: tokens.colorNeutralForeground1,
    textDecoration: "none",
    fontSize: tokens.fontSizeBase300,
    ":hover": {
      backgroundColor: tokens.colorNeutralBackground1Hover,
      ...shorthands.borderColor(tokens.colorBrandStroke1),
    },
  },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  muted: { color: tokens.colorNeutralForeground3 },
});

export function ProjectDashboardPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const navigate = useNavigate();
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
  const remove = useMutation({
    mutationFn: () => deleteProject(projectId, client),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
      void navigate("/projects");
    },
    onError: () => {},
  });
  const removeErrorMessage =
    remove.isError
      ? "Delete failed — this project has generated data that cannot yet be removed automatically. Delete support for projects with pipeline data is coming soon."
      : null;

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
    <div className={styles.stack}>
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
                icon={<PlayRegular />}
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
                icon={<DismissCircleRegular />}
                disabled={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                {cancel.isPending ? "Cancelling…" : "Cancel the workflow"}
              </Button>
            )}
            <Button
              appearance="subtle"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm(`Delete "${project.data?.name ?? "this project"}"? This cannot be undone.`)) {
                  remove.mutate();
                }
              }}
            >
              {remove.isPending ? "Deleting…" : "Delete project"}
            </Button>
          </div>
        }
      />

      {start.isError && <ErrorState error={start.error} />}
      {cancel.isError && <ErrorState error={cancel.error} />}
      {removeErrorMessage && <Body1 as="p">{removeErrorMessage}</Body1>}

      <StatTiles>
        <StatTile
          label="Stage"
          value={formatStage(workflowData?.current_stage ?? null)}
          hint={started ? "Current pipeline position" : "Not started yet"}
          tone="brand"
        />
        <StatTile
          label="Shots locked"
          value={
            workflowData === undefined
              ? "—"
              : `${workflowData.completed_shot_count} / ${workflowData.total_shot_count}`
          }
          hint={
            workflowData?.progress_percentage === null || workflowData === undefined
              ? "Not measurable yet"
              : `${workflowData.progress_percentage.toFixed(0)}% complete`
          }
        />
        <StatTile
          label="Elapsed"
          value={formatDurationSeconds(workflowData?.elapsed_seconds)}
          hint="Since the workflow started"
        />
        <StatTile
          label="Committed cost"
          value={formatMoney(costs.data?.committedAmount)}
          hint={
            costs.data?.hardCap === undefined
              ? "Awaiting the cost ledger"
              : `of ${formatMoney(costs.data.hardCap)} hard cap`
          }
        />
        <StatTile
          label="Open failures"
          value={failures.data?.items.length ?? "—"}
          hint={
            (failures.data?.items.length ?? 0) === 0
              ? "Nothing has failed"
              : "See the failures panel"
          }
          tone={(failures.data?.items.length ?? 0) > 0 ? "danger" : "success"}
        />
      </StatTiles>

      <div className={styles.nextStep}>
        <span className={styles.nextStepIcon} aria-hidden="true">
          <LightbulbFilamentRegular />
        </span>
        <span className={styles.nextStepText}>
          <Caption1 className={styles.nextStepLabel}>Next step</Caption1>
          <Body1>{nextAction(started, workflowData?.current_stage ?? null)}</Body1>
        </span>
        <span className={styles.links}>
          <Link className={styles.quickLink} to={`/projects/${projectId}/transcript`}>
            Review the transcript <ArrowRightRegular aria-hidden="true" />
          </Link>
          <Link className={styles.quickLink} to={`/projects/${projectId}/script`}>
            Review the script <ArrowRightRegular aria-hidden="true" />
          </Link>
          <Link className={styles.quickLink} to={`/projects/${projectId}/storyboard`}>
            Review the storyboard <ArrowRightRegular aria-hidden="true" />
          </Link>
          <Link className={styles.quickLink} to={`/projects/${projectId}/review`}>
            Final review <ArrowRightRegular aria-hidden="true" />
          </Link>
        </span>
      </div>

      <div className={styles.grid}>
        {workflowData === undefined ? (
          <SectionCard title="Pipeline stages">
            <LoadingState label="Loading workflow status" rows={2} />
          </SectionCard>
        ) : (
          <StageTimeline workflow={workflowData} />
        )}

        <SectionCard title="Visual QA" icon={<EyeRegular />}>
          {visualQa.isPending && <LoadingState label="Loading visual QA" rows={2} />}
          {visualQa.isError && <ErrorState error={visualQa.error} />}
          {visualQa.isSuccess && (
            <>
              <Body1 className={visualQaSummary.total === 0 ? styles.muted : undefined}>
                {visualQaSummary.total === 0
                  ? "No visual-QA result has been recorded yet."
                  : `${visualQaSummary.passed} passed · ${visualQaSummary.failed} blocked · ` +
                    `${visualQaSummary.review} awaiting review`}
              </Body1>
              {visualQaSummary.total > 0 && (
                <StatTiles>
                  <StatTile label="Passed" value={visualQaSummary.passed} tone="success" />
                  <StatTile
                    label="Blocked"
                    value={visualQaSummary.failed}
                    tone={visualQaSummary.failed > 0 ? "danger" : "neutral"}
                  />
                  <StatTile
                    label="Awaiting review"
                    value={visualQaSummary.review}
                    tone={visualQaSummary.review > 0 ? "warning" : "neutral"}
                  />
                </StatTiles>
              )}
              {visualQaSummary.hardFailures > 0 && (
                <Caption1>
                  {visualQaSummary.hardFailures} shot
                  {visualQaSummary.hardFailures === 1 ? " carries" : "s carry"} a hard failure and
                  cannot be rendered.
                </Caption1>
              )}
            </>
          )}
        </SectionCard>

        <div className={styles.wide}>
          {costs.isPending && (
            <SectionCard title="Cost">
              <LoadingState label="Loading costs" rows={2} />
            </SectionCard>
          )}
          {costs.isError && (
            <SectionCard title="Cost">
              <ErrorState error={costs.error} onRetry={() => void costs.refetch()} />
            </SectionCard>
          )}
          {costs.isSuccess && <CostSummary costs={costs.data} />}
        </div>

        <CommandsPanel projectId={projectId} />

        {(failures.isPending || attempts.isPending) && (
          <SectionCard title="Failures and provider attempts">
            <LoadingState label="Loading failures" rows={2} />
          </SectionCard>
        )}
        {failures.isSuccess && attempts.isSuccess && (
          <FailurePanel failures={failures.data.items} attempts={attempts.data.items} />
        )}
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
