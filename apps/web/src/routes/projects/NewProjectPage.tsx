import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Body1,
  Button,
  Caption1,
  Field,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Input,
  Slider,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import {
  ArrowLeftRegular,
  MicRegular,
  MoviesAndTvRegular,
  WalletCreditCardRegular,
} from "@fluentui/react-icons";
import { useCallback, useState, type FormEvent, type JSX } from "react";
import { Link, useNavigate } from "react-router-dom";

import { createProject, type ProjectDetail } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { newIdempotencyKey } from "../../api/client";
import { useApiClient } from "../../app/apiContext";
import { SectionCard } from "../../components/Surface";
import { UploadPanel } from "../../components/UploadPanel";
import { VoiceProfilePicker } from "../../components/VoiceProfilePicker";
import { ErrorState } from "../../components/states";
import { useResumableUpload } from "../../hooks/useResumableUpload";
import { startWorkflow, uploadSubtitle } from "../../api/workflows";

const useStyles = makeStyles({
  // A single-column form reads best on a measured column; letting it run the
  // full 1440px would put the labels and their inputs a screen apart.
  page: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    maxWidth: "760px",
    margin: "0 auto",
  },
  header: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
  back: {
    display: "inline-flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalXS,
    alignSelf: "flex-start",
    color: tokens.colorNeutralForeground3,
    textDecoration: "none",
    fontSize: tokens.fontSizeBase200,
    ":hover": { color: tokens.colorBrandForegroundLink, textDecoration: "underline" },
  },
  title: {
    fontSize: tokens.fontSizeHero800,
    lineHeight: tokens.lineHeightHero800,
    fontWeight: tokens.fontWeightSemibold,
    letterSpacing: "-0.02em",
    margin: 0,
  },
  subtitle: { color: tokens.colorNeutralForeground3 },
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalL },
  actions: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap" },
});

const MIN_DURATION = 60;
const MAX_DURATION = 900;

/** An exact, non-negative decimal amount with at most six decimal places. */
const AMOUNT_PATTERN = /^\d{1,12}(\.\d{1,6})?$/;

function amountError(value: string, label: string): string | null {
  return AMOUNT_PATTERN.test(value.trim())
    ? null
    : `Enter ${label} as an amount in dollars, for example 25.00.`;
}

/**
 * Compare two validated amounts without going through a binary float.
 *
 * `AMOUNT_PATTERN` allows up to eighteen digits, more than `Number` can hold
 * exactly, so `Number(a) < Number(b)` would let some inverted pairs through.
 * Padding both sides to a common width makes a plain string compare exact.
 */
function isLess(left: string, right: string): boolean {
  const split = (value: string): [string, string] => {
    const [whole, fraction = ""] = value.trim().split(".");
    return [whole ?? "0", fraction];
  };
  const [leftWhole, leftFraction] = split(left);
  const [rightWhole, rightFraction] = split(right);
  const wholeWidth = Math.max(leftWhole.length, rightWhole.length);
  const fractionWidth = Math.max(leftFraction.length, rightFraction.length);
  const pad = (whole: string, fraction: string): string =>
    whole.padStart(wholeWidth, "0") + fraction.padEnd(fractionWidth, "0");
  return pad(leftWhole, leftFraction) < pad(rightWhole, rightFraction);
}

export function NewProjectPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [targetDuration, setTargetDuration] = useState(300);
  const [visualStyle, setVisualStyle] = useState("flat editorial cartoon");
  const [humorIntensity, setHumorIntensity] = useState(5);
  // Kept as the text the owner typed. Parsing to a number here would round the
  // spend limit before it ever reached the ledger, which stores exact decimals.
  const [warningCap, setWarningCap] = useState("0.00");
  const [hardCap, setHardCap] = useState("0.00");
  const [nameError, setNameError] = useState<string | null>(null);
  // Tracked per field so an invalid warning cap is flagged on the warning cap.
  const [warningCapError, setWarningCapError] = useState<string | null>(null);
  const [hardCapError, setHardCapError] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  // T12 narration resolves the project's voice, and the workflow refuses to
  // start without one. Tracking it here means the start button is disabled with
  // an explanation rather than failing after the click.
  const [voiceProfileId, setVoiceProfileId] = useState<string | null>(null);
  const [subtitleAssetId, setSubtitleAssetId] = useState<string | null>(null);
  const [subtitleFilename, setSubtitleFilename] = useState<string | null>(null);
  const [subtitleError, setSubtitleError] = useState<string | null>(null);

  const upload = useResumableUpload({ client });

  const create = useMutation({
    mutationFn: () =>
      createProject(
        {
          name: name.trim(),
          target_duration_seconds: targetDuration,
          visual_style: visualStyle.trim(),
          humor_intensity: humorIntensity,
          budget_warning_cap: warningCap.trim(),
          budget_hard_cap: hardCap.trim(),
        },
        client,
      ).then((response) => response.data),
    onSuccess: (created) => {
      setProject(created);
      setVoiceProfileId(created.voice_profile_id);
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
    },
  });

  const start = useMutation({
    mutationFn: (projectId: string) =>
      startWorkflow(
        projectId,
        newIdempotencyKey("workflow-start"),
        client,
        subtitleAssetId ? [subtitleAssetId] : undefined,
      ),
    onSuccess: (_, projectId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflow(projectId) });
      void navigate(`/projects/${projectId}`);
    },
  });

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      if (name.trim() === "") {
        setNameError("Give the project a name so you can find it later.");
        return;
      }
      setNameError(null);
      const warningInvalid = amountError(warningCap, "the warning cap");
      const hardInvalid = amountError(hardCap, "the hard cap");
      setWarningCapError(warningInvalid);
      setHardCapError(
        hardInvalid ??
          (warningInvalid === null && isLess(hardCap, warningCap)
            ? "The hard cap must be at least the warning cap."
            : null),
      );
      if (warningInvalid !== null || hardInvalid !== null) {
        return;
      }
      if (isLess(hardCap, warningCap)) {
        return;
      }
      create.mutate();
    },
    [create, hardCap, name, warningCap],
  );

  const uploadComplete = upload.state.phase === "complete";
  const canStart = uploadComplete && voiceProfileId !== null;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <Link className={styles.back} to="/projects">
          <ArrowLeftRegular aria-hidden="true" /> Back to projects
        </Link>
        <h1 className={styles.title}>New project</h1>
        <Caption1 className={styles.subtitle}>
          Name the recap, set how it should look and sound, and cap what it may spend. You can
          upload the source video once the project exists.
        </Caption1>
      </div>
      <form className={styles.form} onSubmit={submit} noValidate>
        <SectionCard title="Basics" icon={<MoviesAndTvRegular />}>
          <Field
            label="Project name"
            required
            validationState={nameError === null ? "none" : "error"}
            validationMessage={nameError ?? undefined}
          >
            <Input
              value={name}
              disabled={project !== null}
              onChange={(_, data) => setName(data.value)}
            />
          </Field>

          <Field
            label={`Target recap duration: ${Math.round(targetDuration / 60)} minutes`}
            hint={`Between ${MIN_DURATION / 60} and ${MAX_DURATION / 60} minutes.`}
          >
            <Slider
              min={MIN_DURATION}
              max={MAX_DURATION}
              step={30}
              value={targetDuration}
              disabled={project !== null}
              onChange={(_, data) => setTargetDuration(data.value)}
            />
          </Field>

          <Field
            label="Visual style"
            hint="Describes the look the storyboard and keyframes aim for."
          >
            <Textarea
              value={visualStyle}
              resize="vertical"
              disabled={project !== null}
              onChange={(_, data) => setVisualStyle(data.value)}
            />
          </Field>

          <Field label={`Humor intensity: ${humorIntensity} of 10`}>
            <Slider
              min={0}
              max={10}
              step={1}
              value={humorIntensity}
              disabled={project !== null}
              onChange={(_, data) => setHumorIntensity(data.value)}
            />
          </Field>
        </SectionCard>

        <SectionCard
          title="Budget"
          icon={<WalletCreditCardRegular />}
          description="Every provider call reserves against these caps before it runs."
        >
        <Field
          label="Warning cap (USD)"
          hint="Where the cost ledger starts warning you. Leave it at 0.00 to be warned from the first generation."
          validationState={warningCapError === null ? "none" : "error"}
          validationMessage={warningCapError ?? undefined}
        >
          <Input
            value={warningCap}
            disabled={project !== null}
            inputMode="decimal"
            onChange={(_, data) => setWarningCap(data.value)}
          />
        </Field>

        <Field
          label="Hard cap (USD)"
          hint={
            "The maximum provider spending allowed for this project. Every image, video, " +
            "narration and QA request reserves against it, and a request that would exceed " +
            "it is refused rather than charged. A project on a deployment with paid " +
            "provider credentials needs a hard cap above 0.00 before its workflow can start."
          }
          validationState={hardCapError === null ? "none" : "error"}
          validationMessage={hardCapError ?? undefined}
        >
          <Input
            value={hardCap}
            disabled={project !== null}
            inputMode="decimal"
            onChange={(_, data) => setHardCap(data.value)}
          />
        </Field>

        </SectionCard>

        {project === null && (
          <div className={styles.actions}>
            <Button appearance="primary" type="submit" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create project"}
            </Button>
            <Button appearance="secondary" onClick={() => void navigate("/projects")}>
              Cancel
            </Button>
          </div>
        )}
        {create.isError && <ErrorState error={create.error} />}
      </form>

      {project !== null && (
        <>
          <MessageBar intent="success">
            <MessageBarBody>
              <MessageBarTitle>Project created</MessageBarTitle>
              "{project.name}" is created. Upload its source video to continue.
            </MessageBarBody>
          </MessageBar>

          <SectionCard title="Narration voice" icon={<MicRegular />}>
            <VoiceProfilePicker
              projectId={project.id}
              onSelected={(selected) => setVoiceProfileId(selected)}
            />
            {voiceProfileId === null && (
              <Body1 as="p" role="status">
                Select a narration voice before starting the workflow.
              </Body1>
            )}
          </SectionCard>

          <SectionCard title="Source video" headingAs="h3">
            <UploadPanel
              upload={upload}
              onFileSelected={(file) => void upload.start(project.id, file)}
            />
          </SectionCard>

          <SectionCard title="Subtitle file (optional)" headingAs="h3">
            <Field
              hint="Upload an SRT file to use instead of auto-transcription."
              validationState={subtitleError === null ? "none" : "error"}
              validationMessage={subtitleError ?? undefined}
            >
              <input
                type="file"
                accept=".srt,text/plain,application/x-subrip"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  setSubtitleError(null);
                  setSubtitleFilename(file.name);
                  void uploadSubtitle(project.id, file, client).then((res) => {
                    setSubtitleAssetId(res.data.asset_id);
                  }).catch(() => {
                    setSubtitleError("Subtitle upload failed. You can still start without it.");
                    setSubtitleAssetId(null);
                  });
                }}
              />
            </Field>
            {subtitleFilename !== null && subtitleAssetId !== null && (
              <Body1 as="p" role="status">
                Subtitle ready: {subtitleFilename}
              </Body1>
            )}
          </SectionCard>

          <div className={styles.actions}>
            <Button
              appearance="primary"
              disabled={!canStart || start.isPending}
              onClick={() => start.mutate(project.id)}
              data-testid="start-workflow"
            >
              {start.isPending ? "Starting…" : "Start the workflow"}
            </Button>
            <Button
              appearance="secondary"
              onClick={() => void navigate(`/projects/${project.id}`)}
            >
              Open the dashboard
            </Button>
          </div>
          {start.isError && <ErrorState error={start.error} />}
        </>
      )}
    </div>
  );
}
