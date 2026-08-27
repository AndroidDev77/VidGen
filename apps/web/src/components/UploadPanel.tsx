import {
  Body1,
  Button,
  Caption1,
  Label,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  ProgressBar,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useRef, useState, type ChangeEvent, type JSX } from "react";

import type { ResumableUpload } from "../hooks/useResumableUpload";
import { formatBytes } from "../state/format";

const useStyles = makeStyles({
  panel: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  row: { display: "flex", gap: tokens.spacingHorizontalM, alignItems: "center", flexWrap: "wrap" },
  meter: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXXS },
});

export interface UploadPanelProps {
  readonly upload: ResumableUpload;
  readonly onFileSelected: (file: File) => void;
  readonly disabled?: boolean;
}

const PHASE_LABELS: Record<string, string> = {
  idle: "No file selected",
  validating: "Checking the file",
  hashing: "Hashing the file",
  uploading: "Uploading parts",
  finalizing: "Finalizing the upload",
  complete: "Upload complete",
  failed: "Upload failed",
  cancelled: "Upload cancelled",
};

/**
 * Select, hash and upload one source video.
 *
 * Hashing and part upload have separate progress meters so a long hash never
 * looks like a stalled upload.
 */
export function UploadPanel({ upload, onFileSelected, disabled }: UploadPanelProps): JSX.Element {
  const styles = useStyles();
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const { state } = upload;
  const active = state.phase === "hashing" || state.phase === "uploading";

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      setSelectedName(file.name);
      onFileSelected(file);
    }
  };

  const hashFraction = state.totalBytes > 0 ? state.bytesHashed / state.totalBytes : 0;
  const uploadFraction = state.totalBytes > 0 ? state.bytesUploaded / state.totalBytes : 0;

  return (
    <section className={styles.panel} aria-labelledby="upload-heading">
      <Subtitle2 as="h2" id="upload-heading">
        Source video
      </Subtitle2>
      {/* A native label/for pairing: Fluent's Field does not adopt a raw file input. */}
      <div className={styles.meter}>
        <Label htmlFor="source-video-input" required>
          Source video file
        </Label>
        <input
          ref={inputRef}
          id="source-video-input"
          type="file"
          accept="video/mp4,video/quicktime"
          aria-describedby="source-video-hint"
          onChange={handleChange}
          disabled={disabled === true || active}
        />
        <Caption1 id="source-video-hint">
          MP4 or QuickTime. The file is hashed in your browser before any bytes are sent.
        </Caption1>
      </div>
      {selectedName !== null && (
        <Caption1>
          {selectedName} — {formatBytes(state.totalBytes)}
        </Caption1>
      )}

      <div className={styles.meter}>
        <Caption1 id="hash-progress-label">
          Hashing: {formatBytes(state.bytesHashed)} of {formatBytes(state.totalBytes)}
        </Caption1>
        <ProgressBar
          value={hashFraction}
          max={1}
          aria-labelledby="hash-progress-label"
        />
      </div>
      <div className={styles.meter}>
        <Caption1 id="upload-progress-label">
          Uploading: {formatBytes(state.bytesUploaded)} of {formatBytes(state.totalBytes)} (
          {state.completedParts.length} of {state.totalParts} parts)
        </Caption1>
        <ProgressBar
          value={uploadFraction}
          max={1}
          aria-labelledby="upload-progress-label"
        />
      </div>

      <div className={styles.row} role="status" aria-live="polite">
        <Body1>{PHASE_LABELS[state.phase] ?? state.phase}</Body1>
        {active && (
          <Button appearance="secondary" onClick={upload.cancel}>
            Cancel upload
          </Button>
        )}
        {(state.phase === "failed" || state.phase === "cancelled") && (
          <Button
            appearance="secondary"
            onClick={() => {
              upload.reset();
              setSelectedName(null);
              if (inputRef.current) {
                inputRef.current.value = "";
              }
            }}
          >
            Start over
          </Button>
        )}
      </div>

      {state.errorMessage !== null && (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>Upload problem</MessageBarTitle>
            {state.errorMessage}
          </MessageBarBody>
        </MessageBar>
      )}
      {state.phase === "complete" && state.result !== null && (
        <MessageBar intent="success">
          <MessageBarBody>
            <MessageBarTitle>Source video ready</MessageBarTitle>
            {formatBytes(state.result.byte_size)} stored and verified. You can now start the
            workflow.
          </MessageBarBody>
        </MessageBar>
      )}
    </section>
  );
}
