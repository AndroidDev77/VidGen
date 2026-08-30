import {
  Badge,
  Body1,
  Button,
  Caption1,
  Dropdown,
  Field,
  Option,
  Subtitle2,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type {
  ScriptProjection,
  ScriptSegmentProjection,
  ScriptSummaryProjection,
} from "@vidgen/contracts";

import type { DraftState } from "../state/editorState";
import { formatTimestamp } from "../state/format";

const useStyles = makeStyles({
  // Editing copy is reading copy: past roughly 110 characters a narration line
  // becomes hard to scan back across, so the editor keeps a measured column
  // even on a wide display.
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

export interface ScriptDraft {
  readonly text: string;
  readonly visualGag: string;
}

export interface ScriptEditorProps {
  readonly script: ScriptProjection;
  readonly versions: readonly ScriptSummaryProjection[];
  readonly drafts: DraftState<ScriptDraft>;
  readonly savingSegmentId: string | null;
  readonly onSave: (segment: ScriptSegmentProjection, draft: ScriptDraft) => void;
  readonly onSelectVersion: (scriptId: string) => void;
}

export function ScriptEditor({
  script,
  versions,
  drafts,
  savingSegmentId,
  onSave,
  onSelectVersion,
}: ScriptEditorProps): JSX.Element {
  const styles = useStyles();
  const summary = script.script;
  return (
    <div className={styles.wrapper}>
      <div className={styles.toolbar}>
        <Field label="Script version">
          <Dropdown
            value={`Version ${summary.version}`}
            selectedOptions={[summary.script_id]}
            onOptionSelect={(_, data) => {
              if (data.optionValue !== undefined && data.optionValue !== summary.script_id) {
                onSelectVersion(data.optionValue);
              }
            }}
          >
            {versions.map((version) => (
              <Option key={version.script_id} value={version.script_id} text={`Version ${version.version}`}>
                Version {version.version}
                {version.selected ? " (selected)" : ""} — {formatTimestamp(version.created_at)}
              </Option>
            ))}
          </Dropdown>
        </Field>
        <Badge appearance="filled" color={script.approved ? "success" : "informative"}>
          {script.approved ? "Approved" : "Draft"}
        </Badge>
        <Caption1>
          {summary.actual_word_count} words of a {summary.target_word_count}-word target
        </Caption1>
      </div>

      <ul className={styles.list}>
        {script.segments.map((segment) => {
          const draft = drafts.get(segment.segment_id) ?? {
            text: segment.text,
            visualGag: segment.visual_gag ?? "",
          };
          const dirty = drafts.drafts.has(segment.segment_id);
          const conflict = drafts.conflicts.has(segment.segment_id);
          return (
            <li key={segment.segment_id} className={styles.segment}>
              <div className={styles.meta}>
                <Subtitle2 as="h3">Beat {segment.sequence + 1}</Subtitle2>
                <Caption1>{segment.word_count} words</Caption1>
                <Caption1>
                  Estimated {(segment.estimated_duration_ms / 1000).toFixed(1)}s
                  {segment.measured_narration_duration_ms !== null &&
                    ` · measured ${(segment.measured_narration_duration_ms / 1000).toFixed(1)}s`}
                </Caption1>
                {segment.joke_annotation_count > 0 && (
                  <Badge appearance="outline">{segment.joke_annotation_count} joke beats</Badge>
                )}
                {segment.locked && <Badge appearance="tint">Locked</Badge>}
                {dirty && (
                  <Badge appearance="tint" color="warning">
                    Unsaved
                  </Badge>
                )}
                {conflict && (
                  <Badge appearance="tint" color="danger">
                    Conflict
                  </Badge>
                )}
              </div>
              <Field label={`Narration text for beat ${segment.sequence + 1}`}>
                <Textarea
                  value={draft.text}
                  resize="vertical"
                  onChange={(_, data) =>
                    drafts.set(segment.segment_id, { ...draft, text: data.value })
                  }
                />
              </Field>
              <Field label={`Visual intent for beat ${segment.sequence + 1}`}>
                <Textarea
                  value={draft.visualGag}
                  resize="vertical"
                  onChange={(_, data) =>
                    drafts.set(segment.segment_id, { ...draft, visualGag: data.value })
                  }
                />
              </Field>
              {conflict && (
                <Body1>
                  This beat changed on the server while you were editing. Your text is kept above;
                  the saved text is now: “{segment.text}”.
                </Body1>
              )}
              <div className={styles.actions}>
                <Button
                  appearance="primary"
                  disabled={!dirty || savingSegmentId === segment.segment_id}
                  onClick={() => onSave(segment, draft)}
                >
                  {savingSegmentId === segment.segment_id ? "Saving…" : "Save beat"}
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
    </div>
  );
}
