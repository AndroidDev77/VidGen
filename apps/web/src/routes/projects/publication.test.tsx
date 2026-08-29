import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  PublicationDetailProjection,
  PublicationGateProjection,
  YouTubeConnectionCollection,
} from "@vidgen/contracts";
import { describe, expect, it, vi } from "vitest";

import { PublicationPanel } from "../../components/PublicationPanel";
import { renderWithProviders } from "../../test/render";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PUBLICATION_ID = "22222222-2222-4222-8222-222222222222";
const CONNECTION_ID = "33333333-3333-4333-8333-333333333333";
const RENDER_ASSET_ID = "44444444-4444-4444-8444-444444444444";
const RUN_ID = "55555555-5555-4555-8555-555555555555";

function connections(
  overrides: Partial<YouTubeConnectionCollection> = {},
): YouTubeConnectionCollection {
  return {
    items: [
      {
        connection_id: CONNECTION_ID,
        channel: {
          channel_id: "UCtestchannel0001",
          title: "VidGen Test Channel",
          thumbnail_url: "https://yt3.example/x.jpg",
          custom_url: "@vidgen",
        },
        status: "connected",
        granted_scopes: [
          "https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
        encryption_key_version: "v1",
        credential_expires_at: null,
        last_verified_at: null,
        error_code: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
    oauth_configured: true,
    production_authentication_available: false,
    ...overrides,
  };
}

function gate(overrides: Partial<PublicationGateProjection> = {}): PublicationGateProjection {
  return {
    project_id: PROJECT_ID,
    allowed: true,
    final_render_asset_id: RENDER_ASSET_ID,
    final_editorial_run_id: RUN_ID,
    approval_id: RUN_ID,
    caption_asset_id: RENDER_ASSET_ID,
    gate_version: "final-gate/1.0",
    failures: [],
    warnings: [],
    row_version: 3,
    ...overrides,
  };
}

function publication(
  overrides: Partial<PublicationDetailProjection> = {},
): PublicationDetailProjection {
  return {
    publication_id: PUBLICATION_ID,
    project_id: PROJECT_ID,
    connection_id: CONNECTION_ID,
    channel_id: "UCtestchannel0001",
    final_render_asset_id: RENDER_ASSET_ID,
    final_editorial_run_id: RUN_ID,
    approval_id: RUN_ID,
    publication_identity: "a".repeat(64),
    metadata_version: 1,
    status: "PRIVATE_READY",
    phase: "VERIFICATION",
    video_id: "vidtest123",
    video_url: "https://www.youtube.com/watch?v=vidtest123",
    total_bytes: 2_097_152,
    confirmed_offset: 2_097_152,
    processing_state: "succeeded",
    caption_status: "succeeded",
    caption_track_id: "captest123",
    thumbnail_status: "succeeded",
    requested_privacy: "private",
    actual_privacy: "private",
    scheduled_publish_at: null,
    contains_synthetic_media: true,
    made_for_kids: false,
    notify_subscribers: false,
    quota_units: 251,
    capability_profile_version: "youtube-data-v3/2026-08",
    publisher_version: "t25/1.0",
    gate_version: "final-gate/1.0",
    render_identity: "b".repeat(64),
    metadata: {
      title: "Season 3 Episode 4 - animated recap",
      description: "An animated recap.",
      tags: ["recap"],
      category_id: "24",
      default_language: "en",
      caption_language: "en",
      caption_track_name: "VidGen recap",
      made_for_kids: false,
      contains_synthetic_media: true,
      embeddable: true,
      notify_subscribers: false,
      requested_privacy: "private",
      scheduled_publish_at: null,
    },
    failure: null,
    row_version: 3,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    assets: [],
    attempts: [],
    ...overrides,
  };
}

const noop = (): void => undefined;

function panel(overrides: Partial<Parameters<typeof PublicationPanel>[0]> = {}) {
  return (
    <PublicationPanel
      connections={connections()}
      gate={gate()}
      publication={publication()}
      busy={false}
      thumbnailChoices={[]}
      onConnect={noop}
      onDisconnect={noop}
      onCreate={noop}
      onSaveDraft={noop}
      onStart={noop}
      onResume={noop}
      onCancel={noop}
      onChangeVisibility={noop}
      {...overrides}
    />
  );
}

describe("PublicationPanel", () => {
  it("shows the connected channel, the real privacy state and the public watch link", () => {
    renderWithProviders(panel());
    expect(screen.getByText("VidGen Test Channel")).toBeInTheDocument();
    expect(screen.getByText("UCtestchannel0001")).toBeInTheDocument();
    expect(screen.getByText("Actual privacy")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "vidtest123" })).toHaveAttribute(
      "href",
      "https://www.youtube.com/watch?v=vidtest123",
    );
  });

  it("shows byte progress, processing, caption and thumbnail status", () => {
    renderWithProviders(panel());
    expect(screen.getByText("2.0 MiB of 2.0 MiB")).toBeInTheDocument();
    expect(screen.getAllByText("succeeded").length).toBeGreaterThan(0);
    expect(screen.getByText(/captest123/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Upload progress" })).toBeInTheDocument();
  });

  it("reports quota units as a rate limit rather than a charge", () => {
    renderWithProviders(panel());
    expect(screen.getByText("251 (rate limit, not a charge)")).toBeInTheDocument();
  });

  it("never renders a token or a resumable upload URL", () => {
    const { container } = renderWithProviders(panel());
    const text = container.textContent ?? "";
    for (const forbidden of ["Bearer", "refresh_token", "access_token", "googleapis.com/upload"]) {
      expect(text).not.toContain(forbidden);
    }
  });

  it("states the private-environment limitation rather than implying it", () => {
    renderWithProviders(panel());
    expect(screen.getByText(/Private environment/)).toBeInTheDocument();
    expect(screen.getByText(/not production multi-tenant OAuth/)).toBeInTheDocument();
  });

  it("shows the synthetic-media disclosure as a disclosure, not a choice", () => {
    renderWithProviders(panel());
    const checkbox = screen.getByRole("checkbox", {
      name: /Contains altered or synthetic media/,
    });
    expect(checkbox).toBeChecked();
    expect(checkbox).toBeDisabled();
  });

  it("explains why a project cannot publish instead of hiding the button", () => {
    renderWithProviders(
      panel({
        publication: null,
        gate: gate({
          allowed: false,
          failures: [
            {
              code: "RENDER_NOT_APPROVED",
              summary: "This render has not been approved in the review UI.",
              retryable: false,
              http_status: null,
              remediation: "Approve the render on the final review page, then publish.",
            },
          ],
        }),
      }),
    );
    expect(
      screen.getByText(/This render has not been approved in the review UI/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create publication draft" })).toBeNull();
  });

  it("offers the visibility action only after processing succeeded", async () => {
    const onChangeVisibility = vi.fn();
    renderWithProviders(panel({ onChangeVisibility }));
    const apply = screen.getByRole("button", { name: "Apply visibility" });
    await userEvent.click(apply);
    expect(onChangeVisibility).toHaveBeenCalledWith("unlisted", null, false);
  });

  it("hides the visibility action while YouTube is still processing", () => {
    renderWithProviders(
      panel({ publication: publication({ processing_state: "processing", status: "PROCESSING" }) }),
    );
    expect(screen.queryByRole("button", { name: "Apply visibility" })).toBeNull();
  });

  it("offers a resume action for an interrupted upload", async () => {
    const onResume = vi.fn();
    renderWithProviders(
      panel({
        publication: publication({
          status: "UPLOADING",
          video_id: null,
          video_url: "",
          confirmed_offset: 524_288,
          processing_state: null,
          caption_status: null,
          thumbnail_status: null,
          actual_privacy: null,
        }),
        onResume,
      }),
    );
    expect(screen.getByText("512.0 KiB of 2.0 MiB")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume upload" }));
    expect(onResume).toHaveBeenCalled();
  });

  it("surfaces an ambiguous completion with its remediation", () => {
    renderWithProviders(
      panel({
        publication: publication({
          status: "HUMAN_REVIEW_REQUIRED",
          video_id: null,
          video_url: "",
          processing_state: null,
          actual_privacy: null,
          failure: {
            code: "AMBIGUOUS_COMPLETION",
            summary: "YouTube's response to the final chunk was lost.",
            retryable: false,
            http_status: null,
            remediation: "Check the channel's uploads before retrying.",
          },
        }),
      }),
    );
    expect(screen.getByText(/response to the final chunk was lost/)).toBeInTheDocument();
    expect(screen.getByText(/Check the channel's uploads/)).toBeInTheDocument();
  });

  it("offers a connect action when no channel is connected", async () => {
    const onConnect = vi.fn();
    renderWithProviders(
      panel({ connections: connections({ items: [] }), publication: null, onConnect }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Connect YouTube channel" }));
    expect(onConnect).toHaveBeenCalled();
  });
});
