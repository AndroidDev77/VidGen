import {
  Body1,
  Caption1,
  Dropdown,
  Field,
  Option,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { JSX } from "react";

import { queryKeys } from "../api/queryKeys";
import { listVoiceProfiles, selectVoiceProfile } from "../api/voiceProfiles";
import { useApiClient } from "../app/apiContext";
import { ErrorState } from "./states";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
});

export interface VoiceProfilePickerProps {
  readonly projectId: string;
  readonly onSelected?: (voiceProfileId: string) => void;
}

/**
 * Choose the project's narration voice.
 *
 * T12 cannot narrate without one, so this is a required setup step rather than
 * an advanced option: without it the workflow refuses to start, and this is
 * where that is fixed. The list is exactly what the deployment can narrate
 * with - a voice whose provider has no credential is never offered.
 */
export function VoiceProfilePicker({
  projectId,
  onSelected,
}: VoiceProfilePickerProps): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const queryClient = useQueryClient();

  const profiles = useQuery({
    queryKey: queryKeys.voiceProfiles(projectId),
    queryFn: ({ signal }) =>
      listVoiceProfiles(projectId, client, signal).then((response) => response.data),
  });

  const select = useMutation({
    mutationFn: (voiceProfileId: string) =>
      selectVoiceProfile(projectId, { voice_profile_id: voiceProfileId }, client),
    onSuccess: (response) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.voiceProfiles(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.voiceProfile(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      onSelected?.(response.data.profile.voice_profile_id);
    },
  });

  // Defensive for the same reason the commands panel is: a surprising response
  // must not take down the setup screen that is meant to fix a bad project.
  const items = Array.isArray(profiles.data?.items) ? profiles.data.items : [];
  const selectedId = profiles.data?.selected_voice_profile_id ?? undefined;
  const selected = items.find((item) => item.voice_profile_id === selectedId);

  return (
    <div className={styles.root}>
      <Field
        label="Narration voice"
        required
        hint="The workflow cannot start until a voice is selected."
        validationState={selectedId === undefined ? "warning" : "success"}
        validationMessage={
          selectedId === undefined ? "Select a narration voice to continue." : undefined
        }
      >
        <Dropdown
          aria-label="Narration voice"
          disabled={profiles.isLoading || select.isPending}
          value={
            selected === undefined
              ? ""
              : `${selected.provider} · ${selected.provider_voice_id}`
          }
          selectedOptions={selectedId === undefined ? [] : [selectedId]}
          onOptionSelect={(_, data) => {
            if (data.optionValue !== undefined) {
              select.mutate(data.optionValue);
            }
          }}
        >
          {items.map((item) => (
            <Option key={item.voice_profile_id} value={item.voice_profile_id}>
              {`${item.provider} · ${item.provider_voice_id}`}
            </Option>
          ))}
        </Dropdown>
      </Field>
      {items.length === 0 && !profiles.isLoading && (
        <Body1 role="alert">
          This deployment has no narration provider configured, so no voice can be selected.
        </Body1>
      )}
      {selected !== undefined && (
        <Caption1>
          {`Model ${selected.model}, ${selected.language}, ${selected.output_format}. `}
          Changing the voice starts a new narration run and rebuilds everything below it.
        </Caption1>
      )}
      {profiles.isError && <ErrorState error={profiles.error} />}
      {select.isError && <ErrorState error={select.error} />}
    </div>
  );
}
