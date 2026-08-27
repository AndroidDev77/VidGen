import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  Button,
  Caption1,
  LargeTitle,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { AddRegular } from "@fluentui/react-icons";
import type { JSX } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useApiClient } from "../../app/apiContext";
import { listProjects } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import {
  formatDurationSeconds,
  formatMoney,
  formatStage,
  formatTimestamp,
} from "../../state/format";

const useStyles = makeStyles({
  header: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
    marginBottom: tokens.spacingVerticalL,
    flexWrap: "wrap",
  },
  spacer: { flexGrow: 1 },
  scroll: { overflowX: "auto" },
});

export function ProjectListPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: queryKeys.projects(),
    queryFn: ({ signal }) => listProjects(client, signal).then((response) => response.data),
  });

  return (
    <div>
      <div className={styles.header}>
        <LargeTitle as="h1">Projects</LargeTitle>
        <div className={styles.spacer} />
        <Button
          appearance="primary"
          icon={<AddRegular />}
          onClick={() => void navigate("/projects/new")}
        >
          New project
        </Button>
      </div>

      {query.isPending && <LoadingState label="Loading your projects" rows={4} />}
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {query.isSuccess && query.data.length === 0 && (
        <EmptyState
          title="No projects yet"
          description="Create a project and upload a source video to produce your first recap."
          action={
            <Button appearance="primary" onClick={() => void navigate("/projects/new")}>
              Create your first project
            </Button>
          }
        />
      )}
      {query.isSuccess && query.data.length > 0 && (
        <div className={styles.scroll}>
          <Table aria-label="Your projects">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Project</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell>Stage</TableHeaderCell>
                <TableHeaderCell>Target length</TableHeaderCell>
                <TableHeaderCell>Style</TableHeaderCell>
                <TableHeaderCell>Humor</TableHeaderCell>
                <TableHeaderCell>Cost</TableHeaderCell>
                <TableHeaderCell>Last updated</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.map((project) => (
                <TableRow key={project.id}>
                  <TableCell>
                    <Link to={`/projects/${project.id}`}>{project.name}</Link>
                    {project.has_failures && (
                      <Badge appearance="filled" color="danger" aria-label="Has failures">
                        Failures
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={project.status} />
                  </TableCell>
                  <TableCell>
                    {formatStage(project.current_stage)}
                    {project.progress_percentage !== null && (
                      <Caption1> · {project.progress_percentage.toFixed(0)}%</Caption1>
                    )}
                  </TableCell>
                  <TableCell>{formatDurationSeconds(project.target_duration_seconds)}</TableCell>
                  <TableCell>{project.visual_style}</TableCell>
                  <TableCell>{project.humor_intensity} / 10</TableCell>
                  <TableCell>
                    {formatMoney(project.committed_cost_amount)}
                    {project.hard_cap_amount !== null && (
                      <Caption1> of {formatMoney(project.hard_cap_amount)}</Caption1>
                    )}
                  </TableCell>
                  <TableCell>{formatTimestamp(project.updated_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
