import type { JSX } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { FinalReviewPage } from "../routes/projects/FinalReviewPage";
import { NewProjectPage } from "../routes/projects/NewProjectPage";
import { ProjectDashboardPage } from "../routes/projects/ProjectDashboardPage";
import { ProjectListPage } from "../routes/projects/ProjectListPage";
import { ReferencesPage } from "../routes/projects/ReferencesPage";
import { ScriptPage } from "../routes/projects/ScriptPage";
import { StoryboardPage } from "../routes/projects/StoryboardPage";
import { TranscriptPage } from "../routes/projects/TranscriptPage";

export function AppRoutes(): JSX.Element {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/projects" replace />} />
      <Route path="/projects" element={<ProjectListPage />} />
      <Route path="/projects/new" element={<NewProjectPage />} />
      <Route path="/projects/:projectId" element={<ProjectDashboardPage />} />
      <Route path="/projects/:projectId/transcript" element={<TranscriptPage />} />
      <Route path="/projects/:projectId/script" element={<ScriptPage />} />
      <Route path="/projects/:projectId/storyboard" element={<StoryboardPage />} />
      <Route path="/projects/:projectId/references" element={<ReferencesPage />} />
      <Route path="/projects/:projectId/review" element={<FinalReviewPage />} />
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
