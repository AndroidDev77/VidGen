import {
  Body1,
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type { InvalidationSet } from "@vidgen/contracts";

import { humanize } from "../state/format";

const useStyles = makeStyles({
  list: { margin: 0, paddingLeft: tokens.spacingHorizontalXL },
});

export interface ConfirmInvalidationDialogProps {
  readonly open: boolean;
  readonly title: string;
  readonly description: string;
  readonly invalidation: InvalidationSet | null;
  readonly confirmLabel: string;
  readonly busy?: boolean;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

/**
 * Confirm before invalidating downstream work.
 *
 * The dialog names the exact artifacts that will be marked stale, so the user
 * sees the real cost of the change before it is applied. Fluent's `Dialog`
 * traps focus, restores it on close, and closes on Escape.
 */
export function ConfirmInvalidationDialog({
  open,
  title,
  description,
  invalidation,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmInvalidationDialogProps): JSX.Element {
  const styles = useStyles();
  const entries = invalidation?.entries ?? [];
  return (
    <Dialog
      open={open}
      modalType="alert"
      onOpenChange={(_, data) => {
        if (!data.open) {
          onCancel();
        }
      }}
    >
      <DialogSurface aria-describedby="invalidation-description">
        <DialogBody>
          <DialogTitle>{title}</DialogTitle>
          <DialogContent>
            <Body1 as="p" id="invalidation-description">
              {description}
            </Body1>
            {entries.length > 0 && (
              <>
                <Subtitle2 as="h3">These will be marked stale</Subtitle2>
                <ul className={styles.list}>
                  {entries.map((entry) => (
                    <li key={`${entry.resource_type}:${entry.resource_id}`}>
                      <Body1>
                        {entry.label} — {humanize(entry.reason)}
                      </Body1>
                    </li>
                  ))}
                </ul>
                <Body1 as="p">
                  Nothing is deleted: previous results are kept as historical records.
                </Body1>
              </>
            )}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
            <Button appearance="primary" onClick={onConfirm} disabled={busy}>
              {busy ? "Working…" : confirmLabel}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
