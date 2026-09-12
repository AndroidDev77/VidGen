import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { StoryboardPage } from "./StoryboardPage";

const { PROJECT_ID } = fixtures;

function renderStoryboard(route = `/projects/${PROJECT_ID}/storyboard`) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={<StoryboardPage />} />
    </Routes>,
    { route },
  );
}

const shotId = (index: number) => fixtures.storyboard.shots[index]!.shot_id;

/** Shot 2 (index 2) soft-failed its video QA and is waiting on a person. */
function softFailedShot(): void {
  server.use(
    http.get(/\/api\/v1\/projects\/[^/]+\/visual-qa$/, () =>
      HttpResponse.json({
        project_id: PROJECT_ID,
        items: [
          fixtures.visualQaRun(2, {
            shot_id: shotId(2),
            outcome: "FAIL",
            hard_failure: false,
            repair_codes: ["PROMPT_TOO_COMPLEX"],
          }),
        ],
      }),
    ),
  );
}

/**
 * Serve the storyboard with one shot carrying a durable control command, the
 * way the projection does once a decision has been recorded against it.
 */
function shotWithCommand(
  index: number,
  overrides: Parameters<typeof fixtures.shotCommand>[0],
): void {
  const shots = fixtures.storyboard.shots.map((shot, position) =>
    position === index ? { ...shot, pending_command: fixtures.shotCommand(overrides) } : shot,
  );
  const project = "http://localhost/api/v1/projects/:projectId";
  server.use(
    http.get(`${project}/storyboard`, () =>
      HttpResponse.json({ ...fixtures.storyboard, shots }),
    ),
    http.get(`${project}/shots/:shotId`, ({ params }) => {
      const position = shots.findIndex((shot) => shot.shot_id === params["shotId"]);
      return HttpResponse.json({
        ...fixtures.shotDetail(Math.max(position, 0)),
        shot: shots[Math.max(position, 0)]!,
      });
    }),
  );
}

/**
 * The window this whole feature exists for.
 *
 * A decision is accepted long before the replacement workflow writes anything,
 * and for that whole window the shot's own rows still describe the failure the
 * reviewer just acted on. The card used to render that as an untouched failure
 * and offer the same button again, so the reviewer could not tell their action
 * had landed and could fire it twice.
 */
describe("a shot with a control command in flight", () => {
  it("does not read as a fresh failure still offering approval", async () => {
    softFailedShot();
    shotWithCommand(2, { command_type: "shot_review_continue", status: "pending" });
    renderStoryboard();

    // The card says what is happening rather than repeating the old failure.
    expect(await screen.findByText("Queued")).toBeVisible();
    expect(
      screen.getByText(/Applying your decision — queued, waiting for the dispatcher\./),
    ).toBeVisible();
    // And the decision it is already applying is not offered a second time.
    const approve = screen.getByRole("button", { name: "Force approve shot 3" });
    expect(approve).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject shot 3" })).toBeDisabled();
  });

  it("separates a dispatched command from one still queued", async () => {
    softFailedShot();
    shotWithCommand(2, {
      command_type: "shot_retry",
      status: "running",
      dispatched: true,
      workflow_id: "vidgen-shot-retry-2",
    });
    renderStoryboard();

    expect(await screen.findByText("Running")).toBeVisible();
    expect(
      screen.getByText(/Retrying — the replacement workflow is running\./),
    ).toBeVisible();
  });

  it("keeps it out of the counts and the bulk decisions above the grid", async () => {
    softFailedShot();
    shotWithCommand(2, { command_type: "shot_review_continue", status: "claimed" });
    renderStoryboard();

    // The card renders, so the storyboard has loaded.
    expect(await screen.findByText("Queued")).toBeVisible();
    // The only soft failure is already being acted on, so nothing is waiting -
    // a bulk force-approve here would enqueue the command a second time.
    expect(screen.queryByRole("region", { name: "Shots awaiting review" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Force approve all failed/ })).toBeNull();
  });

  it("surfaces the error and gives the decision back when the command fails", async () => {
    softFailedShot();
    shotWithCommand(2, {
      command_type: "shot_review_continue",
      status: "failed",
      active: false,
      failure_code: "upstream_identity_moved",
      failure_summary: "the shot's inputs changed before this ran",
      retryable: true,
    });
    renderStoryboard();

    expect(await screen.findByText("Command failed")).toBeVisible();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/the shot's inputs changed before this ran/);
    expect(alert).toHaveTextContent(/This can be tried again\./);
    // The command is over, so the decision is the reviewer's to make again.
    expect(screen.getByRole("button", { name: "Force approve shot 3" })).toBeEnabled();
  });

  it("locks the inspector's own actions while a command is in flight", async () => {
    shotWithCommand(4, { command_type: "shot_regenerate", status: "dispatching" });
    renderStoryboard(`/projects/${PROJECT_ID}/storyboard?shot=${shotId(4)}`);

    const inspector = await screen.findByRole("region", { name: /^Shot 5$/ });
    expect(within(inspector).getByText("Queued")).toBeVisible();
    expect(
      within(inspector).getByRole("button", { name: "Regenerate this shot" }),
    ).toBeDisabled();
    expect(within(inspector).getByRole("button", { name: "Retry failed shot" })).toBeDisabled();
    expect(within(inspector).getByRole("button", { name: "Cancel this shot" })).toBeDisabled();
  });

  it("leaves every other shot alone", async () => {
    softFailedShot();
    shotWithCommand(4, { command_type: "shot_retry", status: "pending" });
    renderStoryboard();

    // Shot 3 has no command of its own: its decision stays available.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Force approve shot 3" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Force approve shot 3" }));
    expect(await screen.findByRole("dialog", { hidden: true })).toBeVisible();
  });
});
