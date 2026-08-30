import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Body1,
  Button,
  Field,
  Input,
  LargeTitle,
  Slider,
  Textarea,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useCallback, useState, type FormEvent, type JSX } from "react";
import { useNavigate } from "react-router-dom";

import { createProject, type ProjectDetail } from "../../api/projects";
import { queryKeys } from "../../api/queryKeys";
import { newIdempotencyKey } from "../../api/client";
import { useApiClient } from "../../app/apiContext";
import { UploadPanel } from "../../components/UploadPanel";
import { VoiceProfilePicker } from "../../components/VoiceProfilePicker";
import { ErrorState } from "../../components/states";
import { useResumableUpload } from "../../hooks/useResumableUpload";
import { startWorkflow } from "../../api/workflows";

const useStyles = makeStyles({
  form: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    maxWidth: "720px",
  },
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
  const [budgetError, setBudgetError] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  // T12 narration resolves the project's voice, and the workflow refuses to
  // start without one. Tracking it here means the start button is disabled with
  // an explanation rather than failing after the click.
  const [voiceProfileId, setVoiceProfileId] = useState<string | null>(null);

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
      startWorkflow(projectId, newIdempotencyKey("workflow-start"), client),
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
      const capError =
        amountError(warningCap, "the warning cap") ?? amountError(hardCap, "the hard cap");
      if (capError !== null) {
        setBudgetError(capError);
        return;
      }
      if (Number(hardCap) < Number(warningCap)) {
        setBudgetError("The hard cap must be at least the warning cap.");
        return;
      }
      setBudgetError(null);
      create.mutate();
    },
    [create, hardCap, name, warningCap],
  );

  const uploadComplete = upload.state.phase === "complete";
  const canStart = uploadComplete && voiceProfileId !== null;

  return (
    <div>
      <LargeTitle as="h1">New project</LargeTitle>
      <form className={styles.form} onSubmit={submit} noValidate>
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

        <Field label="Visual style" hint="Describes the look the storyboard and keyframes aim for.">
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

        <Field
          label="Warning cap (USD)"
          hint="Where the cost ledger starts warning you. Leave it at 0.00 to be warned from the first generation."
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
          validationState={budgetError === null ? "none" : "error"}
          validationMessage={budgetError ?? undefined}
        >
          <Input
            value={hardCap}
            disabled={project !== null}
            inputMode="decimal"
            onChange={(_, data) => setHardCap(data.value)}
          />
        </Field>

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
          <Body1 as="p">
            “{project.name}” is created. Upload its source video to continue.
          </Body1>
          <VoiceProfilePicker
            projectId={project.id}
            onSelected={(selected) => setVoiceProfileId(selected)}
          />
          <UploadPanel
            upload={upload}
            onFileSelected={(file) => void upload.start(project.id, file)}
          />
          {voiceProfileId === null && (
            <Body1 as="p" role="status">
              Select a narration voice before starting the workflow.
            </Body1>
          )}
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
