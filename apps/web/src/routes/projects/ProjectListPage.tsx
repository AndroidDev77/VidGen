import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  Button,
  Caption1,
  Input,
  ProgressBar,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { AddRegular, SearchRegular, VideoClipMultipleRegular } from "@fluentui/react-icons";
import { useMemo, useState, type JSX } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useApiClient } from "../../app/apiContext";
import { listProjects } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { StatusBadge } from "../../components/StatusBadge";
import { SectionCard } from "../../components/Surface";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import {
  formatDurationSeconds,
  formatMoney,
  formatStage,
  formatTimestamp,
  humanize,
} from "../../state/format";

const useStyles = makeStyles({
  header: {
    display: "flex",
    alignItems: "flex-end",
    gap: tokens.spacingHorizontalM,
    marginBottom: tokens.spacingVerticalXL,
    flexWrap: "wrap",
  },
  headings: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXXS },
  title: {
    fontSize: tokens.fontSizeHero800,
    lineHeight: tokens.lineHeightHero800,
    fontWeight: tokens.fontWeightSemibold,
    letterSpacing: "-0.02em",
    margin: 0,
  },
  subtitle: { color: tokens.colorNeutralForeground3 },
  spacer: { flexGrow: 1 },
  search: { minWidth: "220px" },
  scroll: { overflowX: "auto" },

  // The table is the page's centre of gravity, so its rows get real affordance:
  // a hover wash, a settled row height, and no wrapping in the meta columns.
  row: {
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover },
  },
  nameCell: { minWidth: "220px" },
  name: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    flexWrap: "wrap",
  },
  nameLink: {
    color: tokens.colorNeutralForeground1,
    fontWeight: tokens.fontWeightSemibold,
    textDecoration: "none",
    ":hover": { color: tokens.colorBrandForegroundLink, textDecoration: "underline" },
  },
  stage: { display: "flex", flexDirection: "column", gap: "2px", minWidth: "140px" },
  muted: { color: tokens.colorNeutralForeground3 },
  numeric: { fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" },
  cost: { display: "flex", flexDirection: "column", gap: "2px", minWidth: "120px" },
  nowrap: { whiteSpace: "nowrap" },
  failureNotice: {
    display: "flex",
    flexDirection: "column",
    gap: "2px",
  },
  failureReason: {
    color: tokens.colorPaletteRedForeground1,
    fontSize: tokens.fontSizeBase200,
  },
});

export function ProjectListPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const navigate = useNavigate();
  const [filter, setFilter] = useState("");
  const query = useQuery({
    queryKey: queryKeys.projects(),
    queryFn: ({ signal }) => listProjects(client, signal).then((response) => response.data),
  });

  const projects = useMemo(() => query.data ?? [], [query.data]);
  // Filtering is local because the list endpoint returns the caller's own
  // projects in full; a round trip per keystroke would buy nothing.
  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (needle === "") {
      return projects;
    }
    return projects.filter(
      (project) =>
        project.name.toLowerCase().includes(needle) ||
        project.visual_style.toLowerCase().includes(needle) ||
        formatStage(project.current_stage).toLowerCase().includes(needle),
    );
  }, [filter, projects]);

  return (
    <div>
      <div className={styles.header}>
        <div className={styles.headings}>
          <h1 className={styles.title}>Projects</h1>
          <Caption1 className={styles.subtitle}>
            {query.isSuccess
              ? `${projects.length} project${projects.length === 1 ? "" : "s"} in this workspace.`
              : "Every recap you have started, and where each one stands."}
          </Caption1>
        </div>
        <div className={styles.spacer} />
        {query.isSuccess && projects.length > 0 && (
          <Input
            className={styles.search}
            value={filter}
            placeholder="Filter by name, style or stage"
            aria-label="Filter projects"
            contentBefore={<SearchRegular />}
            onChange={(_, data) => setFilter(data.value)}
          />
        )}
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
      {query.isSuccess && projects.length === 0 && (
        <EmptyState
          title="No projects yet"
          description="Create a project and upload a source video to produce your first recap."
          icon={<VideoClipMultipleRegular />}
          action={
            <Button appearance="primary" onClick={() => void navigate("/projects/new")}>
              Create your first project
            </Button>
          }
        />
      )}
      {query.isSuccess && projects.length > 0 && visible.length === 0 && (
        <EmptyState
          title="No matching projects"
          description="No project matches that filter. Clear it to see the whole workspace again."
          icon={<SearchRegular />}
          action={
            <Button appearance="secondary" onClick={() => setFilter("")}>
              Clear the filter
            </Button>
          }
        />
      )}
      {query.isSuccess && visible.length > 0 && (
        <SectionCard flush>
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
                {visible.map((project) => (
                  <TableRow key={project.id} className={styles.row}>
                    <TableCell className={styles.nameCell}>
                      <span className={styles.failureNotice}>
                        <span className={styles.name}>
                          <Link className={styles.nameLink} to={`/projects/${project.id}`}>
                            {project.name}
                          </Link>
                          {project.has_failures && (
                            <Badge
                              appearance="tint"
                              shape="rounded"
                              color="danger"
                              aria-label="Has failures"
                            >
                              Failed
                            </Badge>
                          )}
                        </span>
                        {project.has_failures && (project.latest_failure_stage !== null || project.latest_failure_code !== null) && (
                          <Caption1 className={styles.failureReason}>
                            {[
                              project.latest_failure_stage ? humanize(project.latest_failure_stage) : null,
                              project.latest_failure_code ? humanize(project.latest_failure_code) : null,
                            ]
                              .filter(Boolean)
                              .join(" \u2014 ")}
                          </Caption1>
                        )}
                      </span>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={project.status} />
                    </TableCell>
                    <TableCell>
                      <span className={styles.stage}>
                        <span>{formatStage(project.current_stage)}</span>
                        {project.progress_percentage !== null && (
                          <>
                            <ProgressBar
                              value={project.progress_percentage / 100}
                              max={1}
                              shape="rounded"
                              aria-label={`Progress: ${project.progress_percentage.toFixed(0)}%`}
                            />
                            <Caption1 className={styles.muted}>
                              {project.progress_percentage.toFixed(0)}%
                            </Caption1>
                          </>
                        )}
                      </span>
                    </TableCell>
                    <TableCell className={styles.numeric}>
                      {formatDurationSeconds(project.target_duration_seconds)}
                    </TableCell>
                    <TableCell className={styles.muted}>{project.visual_style}</TableCell>
                    <TableCell className={styles.numeric}>
                      {project.humor_intensity} / 10
                    </TableCell>
                    <TableCell>
                      <span className={styles.cost}>
                        <span className={styles.numeric}>
                          {formatMoney(project.committed_cost_amount)}
                        </span>
                        {project.hard_cap_amount !== null && (
                          <Caption1 className={styles.muted}>
                            of {formatMoney(project.hard_cap_amount)}
                          </Caption1>
                        )}
                      </span>
                    </TableCell>
                    <TableCell className={styles.nowrap}>
                      <Caption1 className={styles.muted}>
                        {formatTimestamp(project.updated_at)}
                      </Caption1>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </SectionCard>
      )}
    </div>
  );
}
