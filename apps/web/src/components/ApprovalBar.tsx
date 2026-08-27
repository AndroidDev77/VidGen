import {
  Body1,
  Button,
  Caption1,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX, ReactNode } from "react";
import type { RenderProjection } from "@vidgen/contracts";

import { formatTimestamp } from "../state/format";

const useStyles = makeStyles({
  bar: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
    padding: tokens.spacingVerticalM,
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
  },
  spacer: { flexGrow: 1 },
});

export interface ApprovalBarProps {
  readonly render: RenderProjection;
  readonly busy: boolean;
  readonly onApprove: () => void;
  readonly children?: ReactNode;
}

/** Approve a verified render. Approval records a decision; it never publishes. */
export function ApprovalBar({ render, busy, onApprove, children }: ApprovalBarProps): JSX.Element {
  const styles = useStyles();
  const approval = render.approval;
  const current = approval !== null && approval.applies_to_current_lineage;
  const blocked = render.stale || !render.verified;

  return (
    <div className={styles.bar}>
      {current ? (
        <MessageBar intent="success">
          <MessageBarBody>
            <MessageBarTitle>Approved</MessageBarTitle>
            Approved by {approval.approved_by} on {formatTimestamp(approval.approved_at)}.
          </MessageBarBody>
        </MessageBar>
      ) : (
        <Body1>
          {blocked
            ? render.stale
              ? "This render is stale: something it depends on changed after it was produced."
              : "This render has no successful verification report yet."
            : "This render is complete and verified, and is ready for your approval."}
        </Body1>
      )}
      {approval !== null && !current && (
        <Caption1>
          A previous approval from {formatTimestamp(approval.approved_at)} is kept as a historical
          record but no longer applies to this lineage.
        </Caption1>
      )}
      <div className={styles.spacer} />
      {children}
      <Button
        appearance="primary"
        disabled={busy || blocked || current}
        onClick={onApprove}
        data-testid="approve-render"
      >
        {busy ? "Approving…" : current ? "Already approved" : "Approve render"}
      </Button>
    </div>
  );
}
