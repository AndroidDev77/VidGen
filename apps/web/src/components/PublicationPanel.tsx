import {
  Badge,
  Body1,
  Button,
  Caption1,
  Checkbox,
  Divider,
  Dropdown,
  Field,
  Input,
  Link,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Option,
  ProgressBar,
  Subtitle2,
  Textarea,
  Title3,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type {
  PrivacyState,
  PublicationDetailProjection,
  PublicationGateProjection,
  PublicationMetadataRequest,
  YouTubeConnectionCollection,
} from "@vidgen/contracts";
import { useState, type JSX } from "react";

import { humanize } from "../state/format";
import { TechnicalDetails } from "./TechnicalDetails";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  row: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap", alignItems: "center" },
  actions: { display: "flex", gap: tokens.spacingHorizontalS, flexWrap: "wrap" },
  facts: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    gap: tokens.spacingHorizontalM,
  },
  fact: { display: "flex", flexDirection: "column" },
  form: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
});

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "informative"> = {
  PUBLISHED: "success",
  PRIVATE_READY: "success",
  FAILED: "danger",
  CANCELLED: "danger",
  PROCESSING_FAILED: "danger",
  HUMAN_REVIEW_REQUIRED: "warning",
  QUOTA_BLOCKED: "warning",
  REAUTHORIZATION_REQUIRED: "warning",
};

const PRIVACY_OPTIONS: readonly PrivacyState[] = ["private", "unlisted", "public"];

function formatBytes(value: number): string {
  if (value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export interface PublicationPanelProps {
  readonly connections: YouTubeConnectionCollection | null;
  readonly gate: PublicationGateProjection | null;
  readonly publication: PublicationDetailProjection | null;
  readonly busy: boolean;
  readonly thumbnailChoices: readonly { readonly assetId: string; readonly label: string }[];
  readonly onConnect: () => void;
  readonly onDisconnect: (connectionId: string) => void;
  readonly onCreate: (connectionId: string, thumbnailAssetId: string | null) => void;
  readonly onSaveDraft: (metadata: PublicationMetadataRequest) => void;
  readonly onStart: () => void;
  readonly onResume: () => void;
  readonly onCancel: () => void;
  readonly onChangeVisibility: (
    privacy: PrivacyState,
    scheduledPublishAt: string | null,
    notifySubscribers: boolean,
  ) => void;
}

/**
 * The T25 publication panel of the final-review page.
 *
 * Everything shown is a bounded projection from the backend. There is nowhere
 * in this component - or in the data it receives - for an OAuth token or a
 * resumable upload URL, because neither ever leaves the server. The privacy the
 * panel reports as current is the one YouTube returned, never the one that was
 * requested.
 */
export function PublicationPanel({
  connections,
  gate,
  publication,
  busy,
  thumbnailChoices,
  onConnect,
  onDisconnect,
  onCreate,
  onSaveDraft,
  onStart,
  onResume,
  onCancel,
  onChangeVisibility,
}: PublicationPanelProps): JSX.Element {
  const styles = useStyles();
  const connection = connections?.items[0] ?? null;
  const [thumbnailAssetId, setThumbnailAssetId] = useState<string>("");
  const [draft, setDraft] = useState<PublicationMetadataRequest | null>(null);
  const [visibility, setVisibility] = useState<PrivacyState>("unlisted");
  const [scheduledAt, setScheduledAt] = useState<string>("");
  const [notify, setNotify] = useState(false);

  const metadata = draft ?? publication?.metadata ?? null;
  const status = publication?.status ?? "";
  const tone = STATUS_TONE[status] ?? "informative";

  if (!connections) {
    return (
      <section className={styles.wrapper}>
        <Title3>Publish to YouTube</Title3>
        <Caption1>Loading connections…</Caption1>
      </section>
    );
  }

  return (
    <section className={styles.wrapper} aria-label="YouTube publication">
      <Title3>Publish to YouTube</Title3>

      {/* The limitation is stated, never implied by a missing button. */}
      {!connections.production_authentication_available ? (
        <MessageBar intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Private environment</MessageBarTitle>
            This deployment still identifies you with a development header, so it is
            reachable only from inside the private network. Connecting a channel here is
            supported for local development and for an authorized staging user; it is not
            production multi-tenant OAuth.
          </MessageBarBody>
        </MessageBar>
      ) : null}

      {/* -- connection ---------------------------------------------------- */}
      {connection ? (
        <div className={styles.row}>
          <Badge appearance="filled" color={connection.status === "connected" ? "success" : "warning"}>
            {humanize(connection.status)}
          </Badge>
          <Body1>{connection.channel.title || connection.channel.channel_id}</Body1>
          <Caption1>{connection.channel.channel_id}</Caption1>
          <div className={styles.actions}>
            <Button size="small" onClick={onConnect} disabled={busy}>
              Reconnect
            </Button>
            <Button
              size="small"
              appearance="subtle"
              onClick={() => onDisconnect(connection.connection_id)}
              disabled={busy}
            >
              Disconnect
            </Button>
          </div>
        </div>
      ) : (
        <div className={styles.row}>
          <Body1>No YouTube channel is connected.</Body1>
          <Button
            appearance="primary"
            onClick={onConnect}
            disabled={busy || !connections.oauth_configured}
          >
            Connect YouTube channel
          </Button>
          {!connections.oauth_configured ? (
            <Caption1>
              Set VIDGEN_YOUTUBE_OAUTH_CLIENT_ID and the token-encryption key first.
            </Caption1>
          ) : null}
        </div>
      )}

      {/* -- eligibility --------------------------------------------------- */}
      {gate && !gate.allowed ? (
        <MessageBar intent="error">
          <MessageBarBody>
            <MessageBarTitle>This render cannot be published yet</MessageBarTitle>
            <ul>
              {gate.failures.map((failure) => (
                <li key={`${failure.code}-${failure.summary}`}>
                  {failure.summary}
                  {failure.remediation ? ` ${failure.remediation}` : ""}
                </li>
              ))}
            </ul>
          </MessageBarBody>
        </MessageBar>
      ) : null}
      {gate?.warnings.length ? (
        <MessageBar intent="info">
          <MessageBarBody>{gate.warnings.join(" ")}</MessageBarBody>
        </MessageBar>
      ) : null}

      {/* -- draft --------------------------------------------------------- */}
      {!publication && connection && gate?.allowed ? (
        <div className={styles.row}>
          <Field label="Thumbnail">
            <Dropdown
              value={
                thumbnailChoices.find((choice) => choice.assetId === thumbnailAssetId)?.label ??
                "YouTube default"
              }
              selectedOptions={thumbnailAssetId ? [thumbnailAssetId] : []}
              onOptionSelect={(_, data) => setThumbnailAssetId(data.optionValue ?? "")}
            >
              <Option value="">YouTube default</Option>
              {thumbnailChoices.map((choice) => (
                <Option key={choice.assetId} value={choice.assetId}>
                  {choice.label}
                </Option>
              ))}
            </Dropdown>
          </Field>
          <Button
            appearance="primary"
            disabled={busy}
            onClick={() => onCreate(connection.connection_id, thumbnailAssetId || null)}
          >
            Create publication draft
          </Button>
        </div>
      ) : null}

      {publication && metadata ? (
        <>
          <Divider />
          <div className={styles.row}>
            <Badge appearance="filled" color={tone}>
              {humanize(status)}
            </Badge>
            <Caption1>
              metadata v{publication.metadata_version} · {publication.phase}
            </Caption1>
          </div>

          {/* The user reviews and edits before anything is uploaded. The draft
              is never regenerated over these edits on a reload or a resume. */}
          <div className={styles.form}>
            <Field label="Title">
              <Input
                value={metadata.title}
                maxLength={100}
                onChange={(_, data) => setDraft({ ...metadata, title: data.value })}
              />
            </Field>
            <Field label="Description">
              <Textarea
                value={metadata.description}
                maxLength={5000}
                onChange={(_, data) => setDraft({ ...metadata, description: data.value })}
              />
            </Field>
            <Field label="Tags (comma separated)">
              <Input
                value={metadata.tags.join(", ")}
                onChange={(_, data) =>
                  setDraft({
                    ...metadata,
                    tags: data.value
                      .split(",")
                      .map((tag) => tag.trim())
                      .filter((tag) => tag !== ""),
                  })
                }
              />
            </Field>
            <div className={styles.row}>
              <Field label="Category ID">
                <Input
                  value={metadata.category_id}
                  onChange={(_, data) => setDraft({ ...metadata, category_id: data.value })}
                />
              </Field>
              <Field label="Caption language">
                <Input
                  value={metadata.caption_language}
                  onChange={(_, data) =>
                    setDraft({ ...metadata, caption_language: data.value })
                  }
                />
              </Field>
              <Field label="Caption track name">
                <Input
                  value={metadata.caption_track_name}
                  onChange={(_, data) =>
                    setDraft({ ...metadata, caption_track_name: data.value })
                  }
                />
              </Field>
            </div>
            <div className={styles.row}>
              <Checkbox
                label="Made for kids"
                checked={metadata.made_for_kids}
                onChange={(_, data) =>
                  setDraft({ ...metadata, made_for_kids: Boolean(data.checked) })
                }
              />
              {/* Disclosure, not a choice: VidGen output is animated and AI
                  generated, so the field is always sent. */}
              <Checkbox
                label="Contains altered or synthetic media (always disclosed)"
                checked={metadata.contains_synthetic_media}
                disabled
              />
              <Checkbox
                label="Notify subscribers"
                checked={metadata.notify_subscribers}
                onChange={(_, data) =>
                  setDraft({ ...metadata, notify_subscribers: Boolean(data.checked) })
                }
              />
            </div>
            <div className={styles.actions}>
              <Button disabled={busy || draft === null} onClick={() => onSaveDraft(metadata)}>
                Save draft
              </Button>
            </div>
          </div>

          {/* -- progress ---------------------------------------------------- */}
          <div className={styles.facts}>
            <div className={styles.fact}>
              <Caption1>Upload</Caption1>
              <Body1>
                {formatBytes(publication.confirmed_offset)} of{" "}
                {formatBytes(publication.total_bytes)}
              </Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>Processing</Caption1>
              <Body1>{publication.processing_state ?? "not started"}</Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>Captions</Caption1>
              <Body1>
                {publication.caption_status ?? "pending"}
                {publication.caption_track_id ? ` (${publication.caption_track_id})` : ""}
              </Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>Thumbnail</Caption1>
              <Body1>{publication.thumbnail_status ?? "pending"}</Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>Requested privacy</Caption1>
              <Body1>{publication.requested_privacy}</Body1>
            </div>
            <div className={styles.fact}>
              {/* What YouTube reports, never what was asked for. */}
              <Caption1>Actual privacy</Caption1>
              <Body1>{publication.actual_privacy ?? "not uploaded"}</Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>YouTube quota units</Caption1>
              <Body1>{publication.quota_units} (rate limit, not a charge)</Body1>
            </div>
            <div className={styles.fact}>
              <Caption1>Video</Caption1>
              {publication.video_url ? (
                <Link href={publication.video_url} target="_blank" rel="noreferrer">
                  {publication.video_id}
                </Link>
              ) : (
                <Body1>not created</Body1>
              )}
            </div>
          </div>
          {publication.total_bytes > 0 ? (
            <ProgressBar
              value={publication.confirmed_offset / publication.total_bytes}
              aria-label="Upload progress"
            />
          ) : null}

          {publication.failure ? (
            <MessageBar intent={tone === "danger" ? "error" : "warning"}>
              <MessageBarBody>
                <MessageBarTitle>{humanize(publication.failure.code)}</MessageBarTitle>
                {publication.failure.summary}
                {publication.failure.remediation ? ` ${publication.failure.remediation}` : ""}
              </MessageBarBody>
            </MessageBar>
          ) : null}

          <div className={styles.actions}>
            <Button appearance="primary" disabled={busy} onClick={onStart}>
              Start upload
            </Button>
            <Button disabled={busy} onClick={onResume}>
              Resume upload
            </Button>
            <Button
              appearance="subtle"
              disabled={busy || Boolean(publication.video_id)}
              onClick={onCancel}
            >
              Cancel
            </Button>
          </div>

          {/* -- explicit visibility ----------------------------------------- */}
          {publication.video_id && publication.processing_state === "succeeded" ? (
            <>
              <Divider />
              <Subtitle2>Change visibility</Subtitle2>
              <Caption1>
                The video is private until you change it here. Verify the private video
                first: this action is what makes it visible.
              </Caption1>
              <div className={styles.row}>
                <Field label="Visibility">
                  <Dropdown
                    value={visibility}
                    selectedOptions={[visibility]}
                    onOptionSelect={(_, data) =>
                      setVisibility((data.optionValue as PrivacyState) ?? "unlisted")
                    }
                  >
                    {PRIVACY_OPTIONS.map((option) => (
                      <Option key={option} value={option}>
                        {option}
                      </Option>
                    ))}
                  </Dropdown>
                </Field>
                <Field label="Scheduled publish (UTC, optional)">
                  <Input
                    type="datetime-local"
                    value={scheduledAt}
                    onChange={(_, data) => setScheduledAt(data.value)}
                  />
                </Field>
                <Checkbox
                  label="Notify subscribers"
                  checked={notify}
                  onChange={(_, data) => setNotify(Boolean(data.checked))}
                />
                <Button
                  appearance="primary"
                  disabled={busy}
                  onClick={() =>
                    onChangeVisibility(
                      visibility,
                      scheduledAt ? new Date(`${scheduledAt}Z`).toISOString() : null,
                      notify,
                    )
                  }
                >
                  Apply visibility
                </Button>
              </div>
            </>
          ) : null}

          <TechnicalDetails
            title="Publication lineage"
            entries={[
              ["Publication identity", publication.publication_identity],
              ["Render asset", publication.final_render_asset_id],
              ["Render identity", publication.render_identity],
              ["T22 run", publication.final_editorial_run_id],
              ["T22 gate version", publication.gate_version],
              ["Channel", publication.channel_id],
              ["Capability profile", publication.capability_profile_version],
              ["Publisher version", publication.publisher_version],
            ]}
          />
        </>
      ) : null}
    </section>
  );
}
