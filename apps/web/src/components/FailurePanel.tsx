import {
  Badge,
  Body1,
  Button,
  Caption1,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { ArrowCounterclockwiseRegular, ShieldTaskRegular } from "@fluentui/react-icons";
import type { JSX } from "react";
import type { PipelineFailureListItem, ProviderAttemptListItem } from "@vidgen/contracts";

import { formatStage, formatTimestamp, humanize } from "../state/format";
import { EmptyState } from "./states";
import { SectionCard } from "./Surface";

const useStyles = makeStyles({
  scroll: { overflowX: "auto" },
  subheading: {
    color: tokens.colorNeutralForeground3,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    fontWeight: tokens.fontWeightSemibold,
    marginTop: tokens.spacingVerticalS,
  },
  muted: { color: tokens.colorNeutralForeground3 },
  nowrap: { whiteSpace: "nowrap" },
  errorMessage: {
    color: tokens.colorPaletteRedForeground1,
    fontStyle: "italic",
  },
  retryRow: {
    display: "flex",
    justifyContent: "flex-end",
    marginBottom: tokens.spacingVerticalS,
  },
});

export interface FailurePanelProps {
  readonly failures: readonly PipelineFailureListItem[];
  readonly attempts: readonly ProviderAttemptListItem[];
  readonly onRetry?: () => void;
  readonly isRetrying?: boolean;
}

export function FailurePanel({ failures, attempts, onRetry, isRetrying }: FailurePanelProps): JSX.Element {
  const styles = useStyles();
  const hasFailedAttempts = attempts.some((a) => a.status === "FAILED" && a.errorMessage);
  return (
    <SectionCard
      title="Failures and provider attempts"
      icon={<ShieldTaskRegular />}
      description={
        failures.length === 0
          ? "Nothing has failed in this project."
          : `${failures.length} recorded failure${failures.length === 1 ? "" : "s"}.`
      }
    >
      {onRetry && hasFailedAttempts && (
        <div className={styles.retryRow}>
          <Button
            appearance="primary"
            icon={<ArrowCounterclockwiseRegular />}
            disabled={isRetrying}
            onClick={onRetry}
          >
            {isRetrying ? "Retrying…" : "Retry"}
          </Button>
        </div>
      )}
      {failures.length === 0 ? (
        <EmptyState
          title="No pipeline failures"
          description="Nothing in this project has failed so far."
        />
      ) : (
        <div className={styles.scroll}>
          <Table aria-label="Recent pipeline failures" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Stage</TableHeaderCell>
                <TableHeaderCell>Error</TableHeaderCell>
                <TableHeaderCell>Classification</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {failures.map((failure) => (
                <TableRow key={failure.id}>
                  <TableCell>{formatStage(failure.stage)}</TableCell>
                  <TableCell>{humanize(failure.errorCode)}</TableCell>
                  <TableCell>
                    <Badge
                      appearance="outline"
                      shape="rounded"
                      color={failure.retryable ? "warning" : "danger"}
                      aria-label={failure.retryable ? "Retryable" : "Not retryable"}
                    >
                      {failure.retryable ? "Retryable" : "Not retryable"}
                    </Badge>
                  </TableCell>
                  <TableCell>{humanize(failure.status)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {attempts.length > 0 && (
        <div className={styles.scroll}>
          <Caption1 as="p" id="attempts-caption" className={styles.subheading}>
            Recent provider attempts
          </Caption1>
          <Table aria-labelledby="attempts-caption" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Provider</TableHeaderCell>
                <TableHeaderCell>Model</TableHeaderCell>
                <TableHeaderCell>Operation</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell>Started</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {attempts.map((attempt) => (
                <TableRow key={attempt.id}>
                  <TableCell>{attempt.provider}</TableCell>
                  <TableCell className={styles.muted}>{attempt.model}</TableCell>
                  <TableCell>{humanize(attempt.operation)}</TableCell>
                  <TableCell>
                    {humanize(attempt.status)}
                    {attempt.errorMessage && (
                      <Body1 as="p" className={styles.errorMessage}>
                        {attempt.errorMessage}
                      </Body1>
                    )}
                  </TableCell>
                  <TableCell className={styles.nowrap}>
                    {formatTimestamp(attempt.startedAt)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </SectionCard>
  );
}
