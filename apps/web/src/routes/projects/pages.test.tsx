import { QueryClient } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { apiError } from "../../test/handlers";
import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { FinalReviewPage } from "./FinalReviewPage";
import { NewProjectPage } from "./NewProjectPage";
import { ProjectDashboardPage } from "./ProjectDashboardPage";
import { ProjectListPage } from "./ProjectListPage";
import { ScriptPage } from "./ScriptPage";
import { StoryboardPage } from "./StoryboardPage";
import { TranscriptPage } from "./TranscriptPage";

const BASE = "http://localhost";
const { PROJECT_ID } = fixtures;

function renderProjectRoute(element: JSX.Element, route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={element} />
    </Routes>,
    { route },
  );
}

describe("ProjectListPage", () => {
  it("shows status, stage, cost and a failure indicator", async () => {
    renderWithProviders(<ProjectListPage />, { route: "/projects" });
    expect(await screen.findByRole("link", { name: "Season 3 Episode 4" })).toBeVisible();
    expect(screen.getAllByText("Review").length).toBeGreaterThan(0);
    expect(screen.getByText("$1.00")).toBeVisible();
    expect(screen.getByLabelText("Has failures")).toBeVisible();
  });

  it("renders an empty state when there are no projects", async () => {
    server.use(http.get(`${BASE}/api/v1/projects`, () => HttpResponse.json([])));
    renderWithProviders(<ProjectListPage />, { route: "/projects" });
    expect(await screen.findByText("No projects yet")).toBeVisible();
  });

  it("renders a structured error state and offers a retry", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects`, () =>
        HttpResponse.json(apiError("internal_error", "Something broke.", { retryable: true }), {
          status: 500,
        }),
      ),
    );
    renderWithProviders(<ProjectListPage />, { route: "/projects" });
    expect(await screen.findByText("Something broke.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  it("shows a loading state before the data arrives", () => {
    renderWithProviders(<ProjectListPage />, { route: "/projects" });
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("NewProjectPage", () => {
  it("requires a project name", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewProjectPage />, { route: "/projects/new" });
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(
      await screen.findByText("Give the project a name so you can find it later."),
    ).toBeVisible();
  });

  it("labels every field accessibly", () => {
    renderWithProviders(<NewProjectPage />, { route: "/projects/new" });
    expect(screen.getByLabelText(/Project name/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Target recap duration/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Visual style/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Humor intensity/)).toBeInTheDocument();
  });

  it("offers the upload panel once the project exists", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NewProjectPage />, { route: "/projects/new" });
    await user.type(screen.getByLabelText(/Project name/), "My recap");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByLabelText(/Source video file/)).toBeInTheDocument();
    expect(screen.getByTestId("start-workflow")).toBeDisabled();
  });
});

describe("ProjectDashboardPage", () => {
  it("renders the stage timeline, cost summary and failures", async () => {
    renderProjectRoute(<ProjectDashboardPage />, `/projects/${PROJECT_ID}`);
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Pipeline stages" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Cost" })).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "Failures and provider attempts" }),
    ).toBeVisible();
    expect(screen.getByText("Provider timeout")).toBeVisible();
    expect(screen.getByLabelText("Retryable")).toBeVisible();
  });

  it("omits a progress percentage the backend could not compute", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/workflow`, () =>
        HttpResponse.json({
          ...fixtures.workflowStatus,
          progress_percentage: null,
          total_shot_count: 0,
          completed_shot_count: 0,
        }),
      ),
    );
    renderProjectRoute(<ProjectDashboardPage />, `/projects/${PROJECT_ID}`);
    expect(
      await screen.findByText("Shot progress is not measurable yet, so no percentage is shown."),
    ).toBeVisible();
  });

  it("links to every review page", async () => {
    renderProjectRoute(<ProjectDashboardPage />, `/projects/${PROJECT_ID}`);
    for (const name of [
      "Review the transcript",
      "Review the script",
      "Review the storyboard",
      "Final review",
    ]) {
      expect(await screen.findByRole("link", { name })).toBeVisible();
    }
  });
});

describe("TranscriptPage", () => {
  it("lists segments with timings, speakers and confidence", async () => {
    renderProjectRoute(<TranscriptPage />, `/projects/${PROJECT_ID}/transcript`);
    expect(await screen.findByRole("heading", { name: "Segment 1" })).toBeVisible();
    expect(screen.getByLabelText("Confidence 94 percent")).toBeVisible();
    expect(screen.getByDisplayValue("SPEAKER_00")).toBeVisible();
  });

  it("filters segments by search text", async () => {
    const user = userEvent.setup();
    renderProjectRoute(<TranscriptPage />, `/projects/${PROJECT_ID}/transcript`);
    await screen.findByRole("heading", { name: "Segment 1" });
    await user.type(screen.getByRole("searchbox", { name: "Search the transcript" }), "suspect");
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Segment 1" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("heading", { name: "Segment 2" })).toBeVisible();
  });

  it("confirms before saving an edit that invalidates downstream work", async () => {
    const user = userEvent.setup();
    let received: unknown = null;
    server.use(
      http.patch(
        `${BASE}/api/v1/projects/:projectId/transcript/segments/:segmentId`,
        async ({ request }) => {
          received = await request.json();
          return HttpResponse.json({
            segment: { ...fixtures.transcript.segments[0], text: "Fixed.", row_version: 2 },
            transcript_row_version: 4,
            invalidation: { schema_version: "1.0", entries: [], requires_confirmation: false },
          });
        },
      ),
    );
    renderProjectRoute(<TranscriptPage />, `/projects/${PROJECT_ID}/transcript`);
    const textarea = await screen.findByLabelText("Transcript text for segment 1");
    await user.clear(textarea);
    await user.type(textarea, "Fixed.");
    await user.click(screen.getAllByRole("button", { name: "Save segment" })[0]!);

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Editing the transcript makes/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Save the segment" }));
    await waitFor(() => expect(received).toMatchObject({ text: "Fixed." }));
  });

  it("keeps the unsaved draft when the save conflicts", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch(`${BASE}/api/v1/projects/:projectId/transcript/segments/:segmentId`, () =>
        HttpResponse.json(
          apiError("version_conflict", "It changed while you were editing.", {
            current_version: 9,
          }),
          { status: 409 },
        ),
      ),
    );
    renderProjectRoute(<TranscriptPage />, `/projects/${PROJECT_ID}/transcript`);
    const textarea = await screen.findByLabelText("Transcript text for segment 1");
    await user.clear(textarea);
    await user.type(textarea, "My local edit.");
    await user.click(screen.getAllByRole("button", { name: "Save segment" })[0]!);
    await user.click(await screen.findByRole("button", { name: "Save the segment" }));
    expect(await screen.findByText("It changed while you were editing.")).toBeVisible();
    // The local text survives the conflict for comparison.
    expect(screen.getByLabelText("Transcript text for segment 1")).toHaveValue("My local edit.");
  });
});

describe("ScriptPage", () => {
  it("shows the selected version, approval state and beats", async () => {
    renderProjectRoute(<ScriptPage />, `/projects/${PROJECT_ID}/script`);
    expect(await screen.findByRole("heading", { name: "Beat 1" })).toBeVisible();
    expect(screen.getByText("Approved")).toBeVisible();
    expect(screen.getByText("80 words of a 700-word target")).toBeVisible();
  });

  it("shows the downstream invalidation preview after a saved change", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch(`${BASE}/api/v1/projects/:projectId/script-segments/:segmentId`, () =>
        HttpResponse.json({
          segment: { ...fixtures.script.segments[0], text: "Funnier.", row_version: 2 },
          script: { ...fixtures.script.script, version: 2 },
          created_version: true,
          invalidation: {
            schema_version: "1.0",
            requires_confirmation: true,
            entries: [
              {
                schema_version: "1.0",
                resource_type: "render",
                resource_id: fixtures.render.render_job_id,
                label: "Verified render attempt 1",
                reason: "script_edited",
              },
            ],
          },
        }),
      ),
    );
    renderProjectRoute(<ScriptPage />, `/projects/${PROJECT_ID}/script`);
    const textarea = await screen.findByLabelText("Narration text for beat 1");
    await user.clear(textarea);
    await user.type(textarea, "Funnier.");
    await user.click(screen.getAllByRole("button", { name: "Save beat" })[0]!);
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/creates a new script version/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Save the beat" }));
    expect(await screen.findByText("Downstream work is now stale")).toBeVisible();
    expect(screen.getByText(/Verified render attempt 1/)).toBeVisible();
  });

  it("marks a beat as unsaved while it is dirty", async () => {
    const user = userEvent.setup();
    renderProjectRoute(<ScriptPage />, `/projects/${PROJECT_ID}/script`);
    const textarea = await screen.findByLabelText("Narration text for beat 1");
    await user.type(textarea, "!");
    expect(await screen.findByText("Unsaved")).toBeVisible();
  });
});

describe("StoryboardPage", () => {
  it("renders shots in canonical sequence with T13 timing", async () => {
    renderProjectRoute(<StoryboardPage />, `/projects/${PROJECT_ID}/storyboard`);
    const headings = await screen.findAllByRole("heading", { name: /^Shot \d+$/ });
    expect(headings.map((node) => node.textContent)).toEqual(
      Array.from({ length: fixtures.SHOT_COUNT }, (_, index) => `Shot ${index + 1}`),
    );
    expect(screen.getAllByText(/Usable 3.000s/)[0]).toBeVisible();
  });

  it("opens the shot inspector for the selected shot", async () => {
    const shotId = fixtures.storyboard.shots[5]!.shot_id;
    renderProjectRoute(
      <StoryboardPage />,
      `/projects/${PROJECT_ID}/storyboard?shot=${shotId}`,
    );
    const inspector = await screen.findByRole("complementary", { name: "Shot inspector" });
    expect(within(inspector).getByRole("heading", { name: "Shot 6" })).toBeVisible();
    expect(within(inspector).getByRole("heading", { name: "Video attempts" })).toBeVisible();
  });

  it("confirms a regeneration and shows the exact invalidation set", async () => {
    const user = userEvent.setup();
    const shot = fixtures.storyboard.shots[5]!;
    let requests = 0;
    server.use(
      http.post(/\/api\/v1\/projects\/[^/]+\/shots\/[^/]+:regenerate$/, () => {
        requests += 1;
        return HttpResponse.json({
          schema_version: "1.0",
          shot_id: shot.shot_id,
          child_workflow_id: "vidgen-shot-new",
          new_identity_hash: "b".repeat(64),
          previous_identity_hash: "a".repeat(64),
          preserved_attempt_ids: [],
          invalidation: {
            schema_version: "1.0",
            requires_confirmation: true,
            entries: [
              {
                schema_version: "1.0",
                resource_type: "shot",
                resource_id: shot.shot_id,
                label: "Shot 6",
                reason: "shot_regenerated",
              },
              {
                schema_version: "1.0",
                resource_type: "render",
                resource_id: fixtures.render.render_job_id,
                label: "Verified render attempt 1",
                reason: "shot_regenerated",
              },
            ],
          },
          row_version: 2,
        });
      }),
    );
    // A retained cache so the sibling entry survives long enough to inspect.
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: 0 } },
    });
    renderWithProviders(
      <Routes>
        <Route path="/projects/:projectId/*" element={<StoryboardPage />} />
      </Routes>,
      { route: `/projects/${PROJECT_ID}/storyboard?shot=${shot.shot_id}`, queryClient },
    );
    // Seed a sibling shot query so we can prove it is not invalidated.
    const siblingKey = ["projects", PROJECT_ID, "shots", fixtures.storyboard.shots[2]!.shot_id];
    queryClient.setQueryData(siblingKey, fixtures.shotDetail(2));

    await user.click(await screen.findByRole("button", { name: "Regenerate this shot" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Sibling shots keep their locked results/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Regenerate the shot" }));

    expect(await screen.findByText("Regeneration started")).toBeVisible();
    expect(screen.getByText(/Shot 6, Verified render attempt 1/)).toBeVisible();
    expect(requests).toBe(1);
    // The sibling's cached data is untouched.
    expect(queryClient.getQueryState(siblingKey)?.isInvalidated).toBe(false);
  });
});

describe("FinalReviewPage", () => {
  it("previews the verified render with a WebVTT caption track", async () => {
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    const video = await screen.findByTestId("final-render-video");
    expect(video).toBeVisible();
    await waitFor(() => expect(screen.getByTestId("final-render-captions")).toBeInTheDocument());
    expect(screen.getByTestId("final-render-captions")).toHaveAttribute("kind", "captions");
    expect(screen.getByText("Passed")).toBeVisible();
  });

  it("toggles captions through URL state", async () => {
    const user = userEvent.setup();
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    const toggle = await screen.findByTestId("caption-toggle");
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(screen.getByTestId("caption-toggle")).not.toBeChecked());
  });

  it("approves the render and records the approval", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    server.use(
      http.post(`${BASE}/api/v1/projects/:projectId/review:approve`, async ({ request }) => {
        body = await request.json();
        const approval = {
          schema_version: "1.0" as const,
          approval_id: fixtures.uuid(99, 8),
          render_job_id: fixtures.render.render_job_id,
          approved_by: "owner-a",
          approved_at: "2026-08-02T09:00:00Z",
          lineage_hash: fixtures.render.lineage_hash!,
          applies_to_current_lineage: true,
        };
        return HttpResponse.json({ approval, render: { ...fixtures.render, approval } });
      }),
      http.get(`${BASE}/api/v1/projects/:projectId/render`, () =>
        HttpResponse.json(fixtures.render),
      ),
    );
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    await user.click(await screen.findByTestId("approve-render"));
    await waitFor(() =>
      expect(body).toMatchObject({ lineage_hash: fixtures.render.lineage_hash }),
    );
  });

  it("blocks approval of a stale render and explains why", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/render`, () =>
        HttpResponse.json({ ...fixtures.render, stale: true }),
      ),
    );
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    expect(await screen.findByText("This render is stale")).toBeVisible();
    expect(await screen.findByTestId("approve-render")).toBeDisabled();
  });

  it("offers MP4, SRT and WebVTT downloads", async () => {
    const user = userEvent.setup();
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    await user.click(await screen.findByRole("button", { name: "Download" }));
    expect(await screen.findByRole("menuitem", { name: "Final MP4" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "Captions (SRT)" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "Captions (WebVTT)" })).toBeVisible();
  });

  it("refreshes an expired signed URL rather than reusing it", async () => {
    let issued = 0;
    server.use(
      http.get(`${BASE}/api/v1/assets/:assetId/download-url`, ({ params }) => {
        issued += 1;
        return HttpResponse.json({
          asset_id: params.assetId,
          url: `${BASE}/blobs/${String(params.assetId)}?sig=${issued}`,
          expires_in_seconds: 60,
        });
      }),
    );
    const { queryClient } = renderProjectRoute(
      <FinalReviewPage />,
      `/projects/${PROJECT_ID}/review`,
    );
    await screen.findByTestId("final-render-video");
    const first = issued;
    await queryClient.refetchQueries({ queryKey: ["assets"] });
    await waitFor(() => expect(issued).toBeGreaterThan(first));
  });

  it("explains that an unfinished render cannot be previewed", async () => {
    server.use(
      http.get(`${BASE}/api/v1/projects/:projectId/render`, () =>
        HttpResponse.json({ ...fixtures.render, status: "rendering", verified: false }),
      ),
    );
    renderProjectRoute(<FinalReviewPage />, `/projects/${PROJECT_ID}/review`);
    expect(await screen.findByText("The render is not finished")).toBeVisible();
  });
});
