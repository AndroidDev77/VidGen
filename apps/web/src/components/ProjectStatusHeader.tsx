import {
  Badge,
  Body1,
  Breadcrumb,
  BreadcrumbButton,
  BreadcrumbDivider,
  BreadcrumbItem,
  Caption1,
  LargeTitle,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX, ReactNode } from "react";
import { Link } from "react-router-dom";
import type { WorkflowStatusProjection } from "@vidgen/contracts";

import { formatDurationSeconds, formatStage } from "../state/format";
import { StatusBadge } from "./StatusBadge";

const useStyles = makeStyles({
  header: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    marginBottom: tokens.spacingVerticalXL,
  },
  row: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
  },
  spacer: { flexGrow: 1 },
});

export interface ProjectStatusHeaderProps {
  readonly projectId: string;
  readonly projectName: string;
  readonly pageTitle: string;
  readonly workflow?: WorkflowStatusProjection | undefined;
  readonly connectionLabel?: string | undefined;
  readonly actions?: ReactNode;
}

export function ProjectStatusHeader({
  projectId,
  projectName,
  pageTitle,
  workflow,
  connectionLabel,
  actions,
}: ProjectStatusHeaderProps): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.header}>
      <Breadcrumb aria-label="Breadcrumb">
        <BreadcrumbItem>
          <BreadcrumbButton as="a" href="/projects" {...{ to: "/projects" }}>
            Projects
          </BreadcrumbButton>
        </BreadcrumbItem>
        <BreadcrumbDivider />
        <BreadcrumbItem>
          <Link to={`/projects/${projectId}`}>{projectName}</Link>
        </BreadcrumbItem>
        <BreadcrumbDivider />
        <BreadcrumbItem>
          <Body1>{pageTitle}</Body1>
        </BreadcrumbItem>
      </Breadcrumb>
      <div className={styles.row}>
        <LargeTitle as="h1">{pageTitle}</LargeTitle>
        {workflow !== undefined && <StatusBadge status={workflow.status} />}
        <div className={styles.spacer} />
        {actions}
      </div>
      {workflow !== undefined && (
        <div className={styles.row}>
          <Caption1>Stage: {formatStage(workflow.current_stage)}</Caption1>
          <Caption1>
            Shots locked: {workflow.completed_shot_count} of {workflow.total_shot_count}
          </Caption1>
          <Caption1>Elapsed: {formatDurationSeconds(workflow.elapsed_seconds)}</Caption1>
          {connectionLabel !== undefined && (
            <Badge appearance="outline" aria-label={`Live updates: ${connectionLabel}`}>
              Live updates: {connectionLabel}
            </Badge>
          )}
        </div>
      )}
    </div>
  );
}
