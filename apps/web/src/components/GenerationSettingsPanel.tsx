import {
  Accordion,
  AccordionHeader,
  AccordionItem,
  AccordionPanel,
  Body1,
  Caption1,
  Checkbox,
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
  // The accordion supplies no surface of its own, so a bare one floats on the
  // page ground between the fields above it.
  surface: {
    borderRadius: tokens.borderRadiusLarge,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
    paddingLeft: tokens.spacingHorizontalS,
    paddingRight: tokens.spacingHorizontalS,
    overflow: "hidden",
  },
  group: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  // The select-all control sits with the dropdown, not above the label, so
  // every stage reads the same way down the panel.
  control: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
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

/**
 * The same, for the T11 script stage. One list covers both of its validators:
 * the plot-compression validator and the recap-script validator (they share
 * UNKNOWN_SOURCE_REFERENCE). Every code either validator emits is listed.
 */
export const SCRIPT_WARN_ONLY_VALIDATION_CODES: ReadonlyArray<{
  readonly value: string;
  readonly description: string;
}> = [
  // Plot compression
  { value: "DUPLICATE_ID", description: "a beat is listed twice" },
  { value: "UNKNOWN_BEAT", description: "a selected or omitted beat is not in the analysis" },
  { value: "MANDATORY_BEAT_OMITTED", description: "a mandatory beat was dropped" },
  { value: "REQUIRED_BEAT_OMITTED", description: "a beat the request required was dropped" },
  {
    value: "STRUCTURAL_BEAT_OMITTED",
    description: "a structural beat (setup, climax, resolution…) was dropped",
  },
  { value: "UNSUPPORTED_BEAT_SUMMARY", description: "a beat summary was altered" },
  { value: "OMISSION_WITHOUT_REASON", description: "a beat was omitted without a reason" },
  { value: "UNKNOWN_SOURCE_REFERENCE", description: "a reference is outside the analysis" },
  { value: "CAUSE_AFTER_EFFECT", description: "a cause beat comes after its effect" },
  { value: "MISSING_CAUSAL_BRIDGE", description: "an omitted cause has no connective explanation" },
  { value: "CYCLIC_BEAT_DEPENDENCY", description: "the selected beats form a cycle" },
  { value: "WORD_BUDGET_OFF_TARGET", description: "per-beat word allocations miss the target" },
  { value: "PACING_OFF_TARGET", description: "the total duration misses the target" },
  // Recap script
  { value: "WORD_COUNT_MISMATCH", description: "the recorded word count is not the actual one" },
  { value: "WORD_COUNT_OUT_OF_RANGE", description: "the word count misses the target" },
  { value: "UNKNOWN_PLOT_BEAT_REFERENCE", description: "a segment cites a beat outside the plan" },
  { value: "UNKNOWN_SCENE_REFERENCE", description: "a segment cites a scene outside the analysis" },
  { value: "UNKNOWN_SPEAKER", description: "a speaker is not an analysis character" },
  {
    value: "ANONYMOUS_SPEAKER_NOT_PERMITTED",
    description: "an anonymous speaker is used when not allowed",
  },
  { value: "INVALID_JOKE_SPAN", description: "a joke span runs past the segment text" },
  { value: "PROHIBITED_PATTERN", description: "a segment matches a forbidden pattern" },
  { value: "NEAR_VERBATIM_TRANSCRIPT", description: "a segment is too close to the transcript" },
  { value: "MANDATORY_BEAT_NOT_COVERED", description: "a mandatory beat has no segment" },
  { value: "BEAT_NOT_COVERED", description: "an optional beat has no segment" },
  { value: "UNKNOWN_CALLBACK_SEGMENT", description: "a callback names a missing segment" },
  { value: "CALLBACK_PAYOFF_BEFORE_SETUP", description: "a payoff comes before its setup" },
  {
    value: "TOO_MUCH_EXPOSITION",
    description: "more than two exposition-only segments in a row at high humor",
  },
  {
    value: "LONG_EXPOSITION_WITHOUT_JOKE",
    description: "over 18 seconds of exposition without a joke at high humor",
  },
  { value: "LOCKED_SEGMENT_CHANGED", description: "a locked segment changed in revision" },
  { value: "COVERAGE_REGRESSED", description: "a revision lost mandatory beat coverage" },
];

/**
 * The same, for the T13 storyboard validator. A tolerated code is recorded on
 * the report at warning severity and never spends a repair attempt.
 */
export const STORYBOARD_WARN_ONLY_VALIDATION_CODES: ReadonlyArray<{
  readonly value: string;
  readonly description: string;
}> = [
  { value: "narration_coverage_gap", description: "shots leave a gap in the narration" },
  { value: "invalid_overlap", description: "shots overlap or share an identity" },
  {
    value: "impossible_duration_allocation",
    description: "the shots cannot be timed to the narration",
  },
  {
    value: "unsupported_provider_duration",
    description: "a shot asks for a duration the provider cannot generate",
  },
  {
    value: "excessive_character_count",
    description: "a shot has more characters than the provider supports",
  },
  {
    value: "too_many_references",
    description: "a shot has more reference images than the provider supports",
  },
  { value: "missing_continuity_state", description: "a shot does not declare its continuity" },
  { value: "invalid_character_reference", description: "a shot cites an unknown character" },
  { value: "invalid_location_reference", description: "a shot cites an unknown location" },
  { value: "missing_evidence_reference", description: "a shot cites evidence that is gone" },
  { value: "provider_schema_failure", description: "the Director's output was malformed" },
  {
    value: "continuity_contradiction",
    description: "continuity drifts between shots without explanation",
  },
  {
    value: "unsupported_camera_movement",
    description: "a camera movement the provider cannot generate",
  },
  { value: "unsupported_transition", description: "a transition the provider cannot generate" },
  { value: "nonpositive_duration", description: "a shot solved to zero duration" },
  { value: "word_range_gap", description: "shots leave a gap in the word ranges" },
];

/**
 * The same, for the T12 narration quality gate. Every code the gate can emit
 * is here: a tolerated one is still measured and recorded on the take's
 * quality report, it just no longer buys another provider attempt.
 */
export const NARRATION_WARN_ONLY_QUALITY_CODES: ReadonlyArray<{
  readonly value: string;
  readonly description: string;
}> = [
  { value: "clipping", description: "the recorded audio clips" },
  { value: "leading_silence", description: "the take starts with too much silence" },
  { value: "trailing_silence", description: "the take ends with too much silence" },
  { value: "internal_silence", description: "the take pauses for too long inside" },
  { value: "speaking_rate", description: "the narrator speaks too fast or too slow" },
  {
    value: "alignment_coverage",
    description: "too little of the approved text was transcribed back",
  },
];

/** The warn-only settings, in the order the stages run. */
type WarnOnlyField =
  | "warn_only_validation_codes"
  | "script_warn_only_validation_codes"
  | "storyboard_warn_only_validation_codes"
  | "narration_warn_only_quality_codes";

interface WarnOnlyGroup {
  readonly field: WarnOnlyField;
  readonly label: string;
  readonly hint: string;
  readonly codes: ReadonlyArray<{ readonly value: string; readonly description: string }>;
}

/**
 * One description per stage, rendered by one component, so the four settings
 * stay identical in look and behaviour as codes are added to any of them.
 */
export const WARN_ONLY_GROUPS: readonly WarnOnlyGroup[] = [
  {
    field: "warn_only_validation_codes",
    label: "Episode analysis",
    hint:
      "A finding with one of these codes is reported and the run continues. Every other " +
      "code fails the analysis and pays to generate it again.",
    codes: WARN_ONLY_VALIDATION_CODES,
  },
  {
    field: "script_warn_only_validation_codes",
    label: "Script",
    hint:
      "Covers both script validators: plot compression and the recap script it writes. " +
      "Every other code fails the step and pays to run it again.",
    codes: SCRIPT_WARN_ONLY_VALIDATION_CODES,
  },
  {
    field: "storyboard_warn_only_validation_codes",
    label: "Storyboard",
    hint:
      "A tolerated finding is recorded on the report and the shot is kept as proposed. " +
      "Every other code sends the segment back for repair, and fails the run once the " +
      "repair attempts are spent.",
    codes: STORYBOARD_WARN_ONLY_VALIDATION_CODES,
  },
  {
    field: "narration_warn_only_quality_codes",
    label: "Narration",
    hint:
      "A tolerated code is still measured and recorded on the take's quality report. " +
      "Every other code fails the take and pays for another provider attempt.",
    codes: NARRATION_WARN_ONLY_QUALITY_CODES,
  },
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

interface WarnOnlyFieldProps {
  readonly group: WarnOnlyGroup;
  readonly selected: readonly string[];
  readonly disabled: boolean;
  readonly onChange: (codes: string[]) => void;
}

/**
 * One stage's warn-only codes: a select-all control and a multiselect list.
 *
 * The select-all checkbox reads "mixed" while some codes are chosen, so the
 * control shows the current state as well as changing it; clicking it takes
 * the stage to all or to none, the two ends an owner actually wants.
 */
function WarnOnlyField({
  group,
  selected,
  disabled,
  onChange,
}: WarnOnlyFieldProps): JSX.Element {
  const styles = useStyles();
  const all = group.codes.length > 0 && selected.length === group.codes.length;
  const some = selected.length > 0 && !all;
  // Long lists make the joined text unreadable, and "every code" is the fact
  // the owner wants at a glance anyway.
  const summary = all ? `All ${group.codes.length} codes` : selected.join(", ");
  return (
    <Field label={group.label} hint={group.hint}>
      <div className={styles.control}>
        <Checkbox
          label={`Select all (${group.codes.length})`}
          aria-label={`Select all ${group.label} codes`}
          disabled={disabled}
          checked={all ? true : some ? "mixed" : false}
          onChange={(_, data) =>
            onChange(data.checked === true ? group.codes.map((code) => code.value) : [])
          }
        />
        <Dropdown
          multiselect
          aria-label={`${group.label}: treat as warnings`}
          placeholder="Nothing tolerated; every code fails"
          disabled={disabled}
          value={summary}
          selectedOptions={[...selected]}
          onOptionSelect={(_, data) => onChange([...data.selectedOptions])}
        >
          {group.codes.map((code) => (
            <Option key={code.value} value={code.value} text={code.value}>
              {`${code.value} — ${code.description}`}
            </Option>
          ))}
        </Dropdown>
      </div>
    </Field>
  );
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
      <Accordion collapsible className={styles.surface}>
        <AccordionItem value="warn-only">
          <AccordionHeader>Treat errors as warnings</AccordionHeader>
          <AccordionPanel>
            <div className={styles.group}>
              <Caption1 className={styles.muted}>
                A finding whose code is chosen here is reported as a warning and the stage
                keeps its output, instead of failing the run and paying to generate it again.
              </Caption1>
              {WARN_ONLY_GROUPS.map((group) => (
                <WarnOnlyField
                  key={group.field}
                  group={group}
                  selected={value[group.field]}
                  disabled={disabled}
                  onChange={(codes) => onChange({ ...value, [group.field]: codes })}
                />
              ))}
            </div>
          </AccordionPanel>
        </AccordionItem>
      </Accordion>
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
