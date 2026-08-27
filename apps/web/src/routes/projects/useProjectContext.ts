import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import type { WorkflowStatusProjection } from "@vidgen/contracts";

import { getProject, type ProjectDetail } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { getWorkflow } from "../../api/workflows";
import { useApiClient } from "../../app/apiContext";
import { useProjectEvents, type ConnectionState } from "../../hooks/useProjectEvents";

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "connecting",
  streaming: "live",
  reconnecting: "reconnecting",
  polling: "polling (live updates unavailable)",
  closed: "paused",
};

export interface ProjectContext {
  readonly projectId: string;
  readonly project: UseQueryResult<ProjectDetail>;
  readonly workflow: UseQueryResult<WorkflowStatusProjection>;
  readonly connectionLabel: string;
}

/** Shared project header data plus the live event subscription. */
export function useProjectContext(options: { readonly events?: boolean } = {}): ProjectContext {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const client = useApiClient();
  const project = useQuery({
    queryKey: queryKeys.project(projectId),
    queryFn: ({ signal }) => getProject(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const workflow = useQuery({
    queryKey: queryKeys.workflow(projectId),
    queryFn: ({ signal }) => getWorkflow(projectId, client, signal).then((r) => r.data),
    enabled: projectId !== "",
  });
  const events = useProjectEvents(projectId, {
    client,
    enabled: (options.events ?? true) && projectId !== "",
  });
  return {
    projectId,
    project,
    workflow,
    connectionLabel: CONNECTION_LABELS[events.connection],
  };
}
