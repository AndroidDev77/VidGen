import {
  Body1,
  Caption1,
  Dropdown,
  Field,
  Input,
  Option,
  Radio,
  RadioGroup,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import type { JSX } from "react";
import type { GenerationCostEstimate, GenerationQuality, ShotPacing } from "@vidgen/contracts";

import { getGenerationEstimate, type GenerationSettingsInput } from "../api/projects";
import { queryKeys } from "../api/queryKeys";
import { useApiClient } from "../app/apiContext";
import { formatMoney } from "../state/format";

const useStyles = makeStyles({
  stack: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  option: { display: "flex", flexDirection: "column", gap: "2px" },
  muted: { color: tokens.colorNeutralForeground3 },
  scroll: { overflowX: "auto" },
  amount: { fontVariantNumeric: "tabular-nums", textAlign: "right", whiteSpace: "nowrap" },
  selected: { fontWeight: tokens.fontWeightSemibold },
});

/** What each strict value means, in the owner's terms. */
export const QUALITY_OPTIONS: ReadonlyArray<{
  readonly value: GenerationQuality;
  readonly label: string;
  readonly description: string;
}> = [
  {
    value: "economy",
    label: "Economy",
    description: "Cheapest. Every shot is animated with Runway Gen-4 Turbo.",
  },
  {
    value: "balanced",
    label: "Balanced",
    description:
      "Gen-4 Turbo normally; Gen-4.5 for hero shots and bounded quality repairs when the " +
      "budget allows.",
  },
  {
    value: "premium",
    label: "Premium",
    description:
      "Gen-4.5 wherever the shot is compatible. The run is refused rather than downgraded " +
      "unless a Turbo fallback is allowed.",
  },
];

export const PACING_OPTIONS: ReadonlyArray<{
  readonly value: ShotPacing;
  readonly label: string;
  readonly description: string;
}> = [
  {
    value: "relaxed",
    label: "Relaxed",
    description: "Fewer, longer shots: about 6 to 10 seconds each.",
  },
  {
    value: "normal",
    label: "Normal (default)",
    description: "About 4 to 7 seconds per shot.",
  },
  {
    value: "fast",
    label: "Fast",
    description: "More, shorter shots: about 2.5 to 4 seconds each.",
  },
];

/**
 * Every deterministic episode-analysis validation code an owner may choose to
 * treat as a warning, with what tolerating it actually means. A code outside
 * this list always fails the run.
 */
export const WARN_ONLY_VALIDATION_CODES: ReadonlyArray<{
  readonly value: string;
  readonly description: string;
}> = [
  { value: "SCENE_SET_MISMATCH", description: "the analysis scenes are not the evidence scenes" },
  { value: "UNSUPPORTED_ALIAS_MERGE", description: "a merged alias has no explicit evidence" },
  { value: "DUPLICATE_ID", description: "two entities share a stable ID" },
  { value: "UNKNOWN_SOURCE_REFERENCE", description: "a reference is outside the evidence" },
  { value: "INVALID_CHRONOLOGY", description: "scene sequences are not unique and monotonic" },
];

const SCENE_THRESHOLD_MIN = 0.1;
const SCENE_THRESHOLD_MAX = 0.9;
const SCENE_THRESHOLD_STEP = 0.05;

function clampSceneThreshold(value: number): number {
  if (Number.isNaN(value)) {
    return SCENE_THRESHOLD_MIN;
  }
  return Math.min(SCENE_THRESHOLD_MAX, Math.max(SCENE_THRESHOLD_MIN, value));
}

export interface GenerationSettingsPanelProps {
  readonly value: GenerationSettingsInput;
  readonly onChange: (next: GenerationSettingsInput) => void;
  readonly disabled?: boolean;
  /** Drives the estimate; the recap length the project targets. */
  readonly targetDurationSeconds: number;
  /** A pre-computed estimate, when the caller already has one. */
  readonly estimate?: GenerationCostEstimate;
}

/**
 * The quality-mode and shot-pacing controls, with the estimated video-generation
 * cost of every mode so the choice is made with a number in view.
 */
export function GenerationSettingsPanel({
  value,
  onChange,
  disabled = false,
  targetDurationSeconds,
  estimate,
}: GenerationSettingsPanelProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const fetched = useQuery({
    queryKey: queryKeys.generationEstimate(targetDurationSeconds, value.shot_pacing),
    queryFn: ({ signal }) =>
      getGenerationEstimate(
        { target_duration_seconds: targetDurationSeconds, shot_pacing: value.shot_pacing },
        client,
        signal,
      ).then((response) => response.data),
    enabled: estimate === undefined && targetDurationSeconds > 0,
  });
  const shown = estimate ?? fetched.data;

  return (
    <div className={styles.stack}>
      <Field label="Generation quality" hint="Which Runway model each shot may use.">
        <RadioGroup
          value={value.generation_quality}
          disabled={disabled}
          onChange={(_, data) =>
            onChange({ ...value, generation_quality: data.value as GenerationQuality })
          }
        >
          {QUALITY_OPTIONS.map((option) => (
            <Radio
              key={option.value}
              value={option.value}
              label={
                <span className={styles.option}>
                  <span>{option.label}</span>
                  <Caption1 className={styles.muted}>{option.description}</Caption1>
                </span>
              }
            />
          ))}
        </RadioGroup>
      </Field>
      {value.generation_quality === "premium" && (
        <Switch
          checked={value.premium_fallback_allowed}
          disabled={disabled}
          label="Allow a Gen-4 Turbo fallback when Gen-4.5 cannot generate or afford a shot"
          onChange={(_, data) => onChange({ ...value, premium_fallback_allowed: data.checked })}
        />
      )}
      <Field
        label="Shot pacing"
        hint="A creative preference for the storyboard, not a fixed number of shots per minute."
      >
        <RadioGroup
          value={value.shot_pacing}
          disabled={disabled}
          onChange={(_, data) => onChange({ ...value, shot_pacing: data.value as ShotPacing })}
        >
          {PACING_OPTIONS.map((option) => (
            <Radio
              key={option.value}
              value={option.value}
              label={
                <span className={styles.option}>
                  <span>{option.label}</span>
                  <Caption1 className={styles.muted}>{option.description}</Caption1>
                </span>
              }
            />
          ))}
        </RadioGroup>
      </Field>
      <Field
        label="Scene detection threshold"
        hint={
          "Lower = more scenes detected (higher API cost); higher = fewer scenes " +
          "(may miss cuts)."
        }
      >
        <Input
          type="number"
          min={SCENE_THRESHOLD_MIN}
          max={SCENE_THRESHOLD_MAX}
          step={SCENE_THRESHOLD_STEP}
          value={String(value.scene_detection_threshold)}
          disabled={disabled}
          onChange={(_, data) => {
            const parsed = Number.parseFloat(data.value);
            if (Number.isNaN(parsed)) {
              return;
            }
            onChange({ ...value, scene_detection_threshold: clampSceneThreshold(parsed) });
          }}
        />
      </Field>
      <Field
        label="Treat as warnings (not errors)"
        hint={
          "Episode-analysis validation findings with these codes are reported and the run " +
          "continues. Every other code fails the analysis and pays to generate it again."
        }
      >
        <Dropdown
          multiselect
          aria-label="Treat as warnings (not errors)"
          placeholder="Nothing tolerated; every code fails"
          disabled={disabled}
          value={value.warn_only_validation_codes.join(", ")}
          selectedOptions={[...value.warn_only_validation_codes]}
          onOptionSelect={(_, data) =>
            onChange({ ...value, warn_only_validation_codes: [...data.selectedOptions] })
          }
        >
          {WARN_ONLY_VALIDATION_CODES.map((code) => (
            <Option key={code.value} value={code.value} text={code.value}>
              {`${code.value} — ${code.description}`}
            </Option>
          ))}
        </Dropdown>
      </Field>
      {fetched.isError && estimate === undefined && (
        <Caption1 className={styles.muted} role="status">
          The cost estimate is unavailable right now; the settings above still apply.
        </Caption1>
      )}
      {shown !== undefined && (
        <div className={styles.stack}>
          <Body1>
            Estimated video-generation cost for a{" "}
            {Math.round(shown.target_duration_seconds / 60)}-minute recap at {shown.shot_pacing}{" "}
            pacing, roughly {shown.estimated_shot_count_low} to {shown.estimated_shot_count_high}{" "}
            shots:
          </Body1>
          <div className={styles.scroll}>
            <Table aria-label="Estimated generation cost by quality mode" size="small">
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Mode</TableHeaderCell>
                  <TableHeaderCell className={styles.amount}>Low</TableHeaderCell>
                  <TableHeaderCell className={styles.amount}>High</TableHeaderCell>
                  <TableHeaderCell className={styles.amount}>vs. economy</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shown.modes.map((mode) => {
                  const selected = mode.generation_quality === value.generation_quality;
                  return (
                    <TableRow
                      key={mode.generation_quality}
                      aria-selected={selected}
                      data-testid={`estimate-${mode.generation_quality}`}
                    >
                      <TableCell className={selected ? styles.selected : undefined}>
                        {mode.generation_quality}
                        {selected ? " (selected)" : ""}
                      </TableCell>
                      <TableCell className={styles.amount}>
                        {formatMoney(mode.estimated_low, shown.currency)}
                      </TableCell>
                      <TableCell className={styles.amount}>
                        {formatMoney(mode.estimated_high, shown.currency)}
                      </TableCell>
                      <TableCell className={styles.amount}>
                        {mode.generation_quality === "economy"
                          ? "—"
                          : `+${formatMoney(mode.delta_from_economy_low, shown.currency)} to ` +
                            `+${formatMoney(mode.delta_from_economy_high, shown.currency)}`}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <Caption1 className={styles.muted}>{shown.notes.join(" ")}</Caption1>
        </div>
      )}
    </div>
  );
}
