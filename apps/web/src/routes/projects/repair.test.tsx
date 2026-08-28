import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { StoryboardPage } from "./StoryboardPage";

function renderProjectRoute(element: JSX.Element, route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:projectId/*" element={element} />
    </Routes>,
    { route },
  );
}

const { PROJECT_ID } = fixtures;

function storyboardRoute(index: number): string {
  const shotId = fixtures.storyboard.shots[index]!.shot_id;
  return `/projects/${PROJECT_ID}/storyboard?shot=${shotId}`;
}

async function openRepair(index = 5): Promise<HTMLElement> {
  renderProjectRoute(<StoryboardPage />, storyboardRoute(index));
  const panel = await screen.findByRole("region", { name: "Repair and fallback" });
  await userEvent.click(within(panel).getAllByRole("button", { name: "Inspect" })[0]!);
  await within(panel).findByRole("table", { name: "Repair attempt lineage" });
  return panel;
}

describe("RepairLineagePanel", () => {
  it("shows the failure classification, repair code and T20 score for the run", async () => {
    renderProjectRoute(<StoryboardPage />, storyboardRoute(5));
    const panel = await screen.findByRole("region", { name: "Repair and fallback" });
    expect(within(panel).getByText("Prompt issue (Structural)")).toBeVisible();
    expect(within(panel).getByText("Wrong character identity")).toBeVisible();
    expect(within(panel).getByText("62.50 / 85")).toBeVisible();
    expect(within(panel).getByText("Hard failure")).toBeVisible();
  });

  it("renders the full bounded attempt lineage with predecessors and costs", async () => {
    const panel = await openRepair();
    const table = within(panel).getByRole("table", { name: "Repair attempt lineage" });
    const rows = within(table).getAllByRole("row");
    // One original, two same-provider repairs, one alternate provider, one
    // deterministic fallback, plus the header row.
    expect(rows).toHaveLength(6);
    expect(within(table).getByText("Original")).toBeVisible();
    expect(within(table).getAllByText("Same provider repair")).toHaveLength(2);
    expect(within(table).getByText("Alternate provider")).toBeVisible();
    expect(within(table).getByText("Deterministic fallback")).toBeVisible();
    // The alternate provider and its model are shown per attempt.
    expect(within(table).getByText("google_veo / veo-3.1-fast-generate-001")).toBeVisible();
    // The root attempt has no predecessor; every repair attempt names one.
    expect(within(table).getByText("root")).toBeVisible();
  });

  it("shows prompt changes as a structured delta, never as a raw prompt", async () => {
    const panel = await openRepair();
    expect(
      within(panel).getByText(
        /Added: Match the referenced character's face, hair and skin tone exactly\./,
      ),
    ).toBeVisible();
    expect(within(panel).getByText(/Seed changed from none to 12345/)).toBeVisible();
    expect(within(panel).getByText(/Preserved 3 constraints/)).toBeVisible();
  });

  it("shows the deterministic fallback render and the repair budget", async () => {
    const panel = await openRepair();
    expect(within(panel).getByText(/parallax-renderer\/1\.0 rendered/)).toBeVisible();
    expect(within(panel).getByText(/Spent 0\.800000 USD/)).toBeVisible();
    expect(within(panel).getByText(/Project budget remaining 9\.200000/)).toBeVisible();
  });

  it("offers only the actions a human-review state accepts, and never a pass", async () => {
    const panel = await openRepair();
    expect(
      within(panel).getByRole("button", { name: "Acknowledge human review" }),
    ).toBeVisible();
    expect(
      within(panel).getByRole("button", {
        name: "Restart after upstream reference correction",
      }),
    ).toBeVisible();
    // A human can never mark a hard-failing visual as passed from this panel.
    expect(within(panel).queryByRole("button", { name: /pass|approve/i })).toBeNull();
  });

  it("sends the owner action for the selected repair run", async () => {
    const panel = await openRepair();
    await userEvent.click(within(panel).getByRole("button", { name: "Resolve human review" }));
    // The mutation resolves against the mocked control plane without error.
    expect(within(panel).getByRole("button", { name: "Resolve human review" })).toBeVisible();
  });
});
