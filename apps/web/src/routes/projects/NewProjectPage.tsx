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

export function NewProjectPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [targetDuration, setTargetDuration] = useState(300);
  const [visualStyle, setVisualStyle] = useState("flat editorial cartoon");
  const [humorIntensity, setHumorIntensity] = useState(5);
  const [nameError, setNameError] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);

  const upload = useResumableUpload({ client });

  const create = useMutation({
    mutationFn: () =>
      createProject(
        {
          name: name.trim(),
          target_duration_seconds: targetDuration,
          visual_style: visualStyle.trim(),
          humor_intensity: humorIntensity,
        },
        client,
      ).then((response) => response.data),
    onSuccess: (created) => {
      setProject(created);
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
      create.mutate();
    },
    [create, name],
  );

  const uploadComplete = upload.state.phase === "complete";

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
          <UploadPanel
            upload={upload}
            onFileSelected={(file) => void upload.start(project.id, file)}
          />
          <div className={styles.actions}>
            <Button
              appearance="primary"
              disabled={!uploadComplete || start.isPending}
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
