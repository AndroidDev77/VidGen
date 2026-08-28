import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  MessageBar,
  MessageBarBody,
  Textarea,
} from "@fluentui/react-components";
import { useState, type JSX } from "react";

export interface VisualQAReviewDialogProps {
  readonly open: boolean;
  readonly decision: "approve" | "reject";
  readonly busy: boolean;
  /** A hard failure can never be cleared by a human; the dialog says so and disables approval. */
  readonly hardFailure: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: (reason: string) => void;
}

/** Records a human decision on an ambiguous `REVIEW` outcome. */
export function VisualQAReviewDialog({
  open,
  decision,
  busy,
  hardFailure,
  onCancel,
  onConfirm,
}: VisualQAReviewDialogProps): JSX.Element {
  const [reason, setReason] = useState("");
  const approving = decision === "approve";
  const blocked = approving && hardFailure;
  return (
    <Dialog open={open} onOpenChange={(_, data) => (data.open ? undefined : onCancel())}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>
            {approving ? "Approve this visual-QA result?" : "Reject this visual-QA result?"}
          </DialogTitle>
          <DialogContent>
            {blocked && (
              <MessageBar intent="error">
                <MessageBarBody>
                  This result carries a hard failure. A hard failure is a measured fact and cannot
                  be cleared by human review.
                </MessageBarBody>
              </MessageBar>
            )}
            <MessageBar intent="info">
              <MessageBarBody>
                Your decision is recorded alongside the automated result. It never changes the
                generated asset and never rewrites the recomputed score.
              </MessageBarBody>
            </MessageBar>
            <Field label="Reason" hint="Recorded with your decision; 500 characters maximum.">
              <Textarea
                value={reason}
                onChange={(_, data) => setReason(data.value.slice(0, 500))}
                aria-label="Reason for this visual-QA decision"
              />
            </Field>
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button
              appearance="primary"
              onClick={() => onConfirm(reason)}
              disabled={busy || blocked}
            >
              {approving ? "Record approval" : "Record rejection"}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
