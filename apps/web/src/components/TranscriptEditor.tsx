import {
  Badge,
  Body1,
  Button,
  Caption1,
  Field,
  Input,
  SearchBox,
  Subtitle2,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useMemo, useState, type JSX } from "react";
import type { TranscriptProjection, TranscriptSegmentProjection } from "@vidgen/contracts";

import type { DraftState } from "../state/editorState";
import { formatDurationSeconds } from "../state/format";

const useStyles = makeStyles({
  // Matches the script editor: a measured column keeps a transcript line
  // scannable instead of running the full width of a desk monitor.
  wrapper: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    maxWidth: "1040px",
  },
  toolbar: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "end" },
  list: { listStyle: "none", margin: 0, padding: 0, display: "grid", gap: tokens.spacingVerticalM },
  segment: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    padding: tokens.spacingHorizontalL,
    borderRadius: tokens.borderRadiusLarge,
    backgroundColor: tokens.colorNeutralBackground1,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    boxShadow: tokens.shadow2,
  },
  meta: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
});

export interface TranscriptDraft {
  readonly text: string;
  readonly speakerLabel: string;
}

export interface TranscriptEditorProps {
  readonly transcript: TranscriptProjection;
  readonly drafts: DraftState<TranscriptDraft>;
  readonly search: string;
  readonly onSearchChange: (value: string) => void;
  readonly savingSegmentId: string | null;
  readonly onSave: (segment: TranscriptSegmentProjection, draft: TranscriptDraft) => void;
  readonly onSeek?: (startSeconds: number) => void;
}

export function TranscriptEditor({
  transcript,
  drafts,
  search,
  onSearchChange,
  savingSegmentId,
  onSave,
  onSeek,
}: TranscriptEditorProps): JSX.Element {
  const styles = useStyles();
  const [focusedIndex, setFocusedIndex] = useState(0);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (needle === "") {
      return transcript.segments;
    }
    return transcript.segments.filter(
      (segment) =>
        segment.text.toLowerCase().includes(needle) ||
        (segment.speaker_label ?? "").toLowerCase().includes(needle),
    );
  }, [search, transcript.segments]);

  return (
    <div className={styles.wrapper}>
      <div className={styles.toolbar}>
        <Field label="Search the transcript">
          <SearchBox
            value={search}
            placeholder="Find words or a speaker"
            onChange={(_, data) => onSearchChange(data.value)}
          />
        </Field>
        <Caption1>
          Version {transcript.version} · coverage {(transcript.coverage_score * 100).toFixed(1)}% ·{" "}
          {formatDurationSeconds(transcript.duration_seconds)}
        </Caption1>
      </div>

      {visible.length === 0 ? (
        <Body1>No transcript segments match “{search}”.</Body1>
      ) : (
        <ul className={styles.list}>
          {visible.map((segment, index) => {
            const draft = drafts.get(segment.segment_id) ?? {
              text: segment.text,
              speakerLabel: segment.speaker_label ?? "",
            };
            const dirty = drafts.drafts.has(segment.segment_id);
            const conflict = drafts.conflicts.get(segment.segment_id);
            return (
              <li key={segment.segment_id} className={styles.segment}>
                <div className={styles.meta}>
                  <Subtitle2 as="h3">Segment {segment.sequence + 1}</Subtitle2>
                  <Caption1>
                    {formatDurationSeconds(segment.start_seconds)} –{" "}
                    {formatDurationSeconds(segment.end_seconds)}
                  </Caption1>
                  {segment.confidence !== null && (
                    <Badge
                      appearance="outline"
                      color={segment.confidence >= 0.8 ? "success" : "warning"}
                      aria-label={`Confidence ${(segment.confidence * 100).toFixed(0)} percent`}
                    >
                      Confidence {(segment.confidence * 100).toFixed(0)}%
                    </Badge>
                  )}
                  {segment.edited && <Badge appearance="tint">Edited</Badge>}
                  {dirty && <Badge appearance="tint" color="warning">Unsaved</Badge>}
                  {onSeek !== undefined && (
                    <Button
                      appearance="subtle"
                      size="small"
                      onClick={() => onSeek(segment.start_seconds)}
                    >
                      Jump to {formatDurationSeconds(segment.start_seconds)}
                    </Button>
                  )}
                </div>
                <Field label={`Speaker for segment ${segment.sequence + 1}`}>
                  <Input
                    value={draft.speakerLabel}
                    onChange={(_, data) =>
                      drafts.set(segment.segment_id, { ...draft, speakerLabel: data.value })
                    }
                  />
                </Field>
                <Field label={`Transcript text for segment ${segment.sequence + 1}`}>
                  <Textarea
                    id={`transcript-text-${segment.segment_id}`}
                    value={draft.text}
                    resize="vertical"
                    onFocus={() => setFocusedIndex(index)}
                    // Alt+Arrow moves between segments without stealing plain
                    // arrow keys from ordinary text editing.
                    onKeyDown={(event) => {
                      if (!event.altKey) {
                        return;
                      }
                      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
                        return;
                      }
                      const delta = event.key === "ArrowDown" ? 1 : -1;
                      const next = Math.min(
                        Math.max(focusedIndex + delta, 0),
                        visible.length - 1,
                      );
                      setFocusedIndex(next);
                      document
                        .getElementById(`transcript-text-${visible[next]?.segment_id}`)
                        ?.focus();
                      event.preventDefault();
                    }}
                    onChange={(_, data) =>
                      drafts.set(segment.segment_id, { ...draft, text: data.value })
                    }
                  />
                </Field>
                {conflict !== undefined && (
                  <Caption1>
                    Your unsaved version is kept above. The saved text is now: “{segment.text}”.
                  </Caption1>
                )}
                <div className={styles.actions}>
                  <Button
                    appearance="primary"
                    disabled={!dirty || savingSegmentId === segment.segment_id}
                    onClick={() => onSave(segment, draft)}
                  >
                    {savingSegmentId === segment.segment_id ? "Saving…" : "Save segment"}
                  </Button>
                  <Button
                    appearance="subtle"
                    disabled={!dirty}
                    onClick={() => drafts.clear(segment.segment_id)}
                  >
                    Discard changes
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
