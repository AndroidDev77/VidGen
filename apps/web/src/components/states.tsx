import {
  Body1,
  Button,
  Caption1,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Skeleton,
  SkeletonItem,
  Spinner,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { ReactNode } from "react";

import { classify, NetworkError, VidGenApiError } from "../api/errors";

const useStyles = makeStyles({
  block: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: tokens.spacingVerticalM,
    padding: tokens.spacingVerticalXXL,
    textAlign: "center",
  },
  skeleton: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    width: "100%",
  },
  row: { display: "flex", gap: tokens.spacingHorizontalS, alignItems: "center" },
});

export interface LoadingStateProps {
  readonly label: string;
  /** Render placeholder rows instead of a spinner for list-shaped content. */
  readonly rows?: number;
}

export function LoadingState({ label, rows }: LoadingStateProps): JSX.Element {
  const styles = useStyles();
  if (rows === undefined) {
    return (
      <div className={styles.block} role="status" aria-live="polite">
        <Spinner size="medium" label={label} />
      </div>
    );
  }
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="vidgen-visually-hidden">{label}</span>
      <Skeleton className={styles.skeleton}>
        {Array.from({ length: rows }, (_, index) => (
          <SkeletonItem key={index} size={40} />
        ))}
      </Skeleton>
    </div>
  );
}

export interface EmptyStateProps {
  readonly title: string;
  readonly description: string;
  readonly action?: ReactNode;
}

export function EmptyState({ title, description, action }: EmptyStateProps): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.block}>
      <Subtitle2 as="h2">{title}</Subtitle2>
      <Body1>{description}</Body1>
      {action}
    </div>
  );
}

export interface ErrorStateProps {
  readonly error: unknown;
  readonly title?: string;
  readonly onRetry?: () => void;
}

/**
 * Render a structured failure.
 *
 * Every message here comes from the API's `ApiError.summary` or a mapped
 * fallback; tracebacks, SQL, provider payloads and FFmpeg logs never appear.
 * The failure kind is stated in words as well as colour.
 */
export function ErrorState({ error, title, onRetry }: ErrorStateProps): JSX.Element {
  const styles = useStyles();
  const kind = classify(error);
  const heading = title ?? KIND_TITLES[kind];
  const detail = describe(error);
  const fields = error instanceof VidGenApiError ? error.error.fields : [];
  const correlationId = error instanceof VidGenApiError ? error.error.correlation_id : null;
  return (
    <MessageBar intent={kind === "retryable" || kind === "network" ? "warning" : "error"}>
      <MessageBarBody>
        <MessageBarTitle>{heading}</MessageBarTitle>
        <Body1 as="p">{detail}</Body1>
        {fields.length > 0 && (
          <ul>
            {fields.map((field) => (
              <li key={`${field.field}:${field.code}`}>
                <Caption1>
                  {field.field}: {field.message}
                </Caption1>
              </li>
            ))}
          </ul>
        )}
        {correlationId !== null && (
          <Caption1 as="p">Reference: {correlationId}</Caption1>
        )}
        {onRetry !== undefined && (
          <div className={styles.row}>
            <Button appearance="secondary" onClick={onRetry}>
              Try again
            </Button>
          </div>
        )}
      </MessageBarBody>
    </MessageBar>
  );
}

const KIND_TITLES: Record<ReturnType<typeof classify>, string> = {
  validation: "Check these details",
  conflict: "This changed while you were editing",
  ownership: "Not available",
  retryable: "Temporary problem",
  lineage: "Out of date with the current lineage",
  budget: "Budget limit reached",
  provider: "A provider is unavailable",
  verification: "Render verification incomplete",
  network: "Cannot reach VidGen",
  unknown: "Something went wrong",
};

export function describe(error: unknown): string {
  if (error instanceof VidGenApiError) {
    return error.error.summary;
  }
  if (error instanceof NetworkError) {
    return "The VidGen API could not be reached. Check that it is running, then try again.";
  }
  return "An unexpected problem occurred. Try again in a moment.";
}
