import {
  Body1,
  Button,
  Caption1,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Skeleton,
  SkeletonItem,
  Spinner,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { DocumentSearchRegular } from "@fluentui/react-icons";
import type { JSX, ReactNode } from "react";

import { classify, NetworkError, VidGenApiError } from "../api/errors";

const useStyles = makeStyles({
  block: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: tokens.spacingVerticalS,
    padding: `${tokens.spacingVerticalXXXL} ${tokens.spacingHorizontalXL}`,
    textAlign: "center",
    borderRadius: tokens.borderRadiusLarge,
    border: `1px dashed ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  spinnerBlock: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: tokens.spacingVerticalM,
    padding: tokens.spacingVerticalXXL,
    textAlign: "center",
  },
  // A tinted glyph gives the empty state a focal point without shipping an
  // illustration that would need its own dark-mode artwork.
  emptyIcon: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: "48px",
    height: "48px",
    marginBottom: tokens.spacingVerticalXS,
    borderRadius: tokens.borderRadiusCircular,
    backgroundColor: tokens.colorBrandBackground2,
    color: tokens.colorBrandForeground2,
    fontSize: tokens.fontSizeHero700,
  },
  emptyBody: { color: tokens.colorNeutralForeground3, maxWidth: "46ch" },
  emptyAction: { marginTop: tokens.spacingVerticalS },
  skeleton: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    width: "100%",
  },
  skeletonRow: { borderRadius: tokens.borderRadiusMedium },
  fields: {
    margin: 0,
    paddingLeft: tokens.spacingHorizontalL,
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalXXS,
  },
  reference: {
    color: tokens.colorNeutralForeground3,
    fontFamily: tokens.fontFamilyMonospace,
  },
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
      <div className={styles.spinnerBlock} role="status" aria-live="polite">
        <Spinner size="medium" label={label} />
      </div>
    );
  }
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="vidgen-visually-hidden">{label}</span>
      <Skeleton className={styles.skeleton}>
        {Array.from({ length: rows }, (_, index) => (
          <SkeletonItem
            key={index}
            className={styles.skeletonRow}
            size={index === 0 ? 32 : 40}
            // A shorter trailing row reads as a list of content rather than as
            // a stack of identical grey bars.
            style={index === rows - 1 ? { width: "62%" } : undefined}
          />
        ))}
      </Skeleton>
    </div>
  );
}

export interface EmptyStateProps {
  readonly title: string;
  readonly description: string;
  readonly action?: ReactNode;
  readonly icon?: ReactNode;
}

export function EmptyState({ title, description, action, icon }: EmptyStateProps): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.block}>
      <span className={styles.emptyIcon} aria-hidden="true">
        {icon ?? <DocumentSearchRegular />}
      </span>
      <Subtitle2 as="h2">{title}</Subtitle2>
      <Body1 className={styles.emptyBody}>{description}</Body1>
      {action !== undefined && <div className={styles.emptyAction}>{action}</div>}
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
    <MessageBar
      intent={kind === "retryable" || kind === "network" ? "warning" : "error"}
      layout="multiline"
    >
      <MessageBarBody>
        <MessageBarTitle>{heading}</MessageBarTitle>
        <Body1 as="p">{detail}</Body1>
        {fields.length > 0 && (
          <ul className={styles.fields}>
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
          <Caption1 as="p" className={styles.reference}>
            Reference: {correlationId}
          </Caption1>
        )}
      </MessageBarBody>
      {onRetry !== undefined && (
        <MessageBarActions>
          <Button appearance="secondary" onClick={onRetry}>
            Try again
          </Button>
        </MessageBarActions>
      )}
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
