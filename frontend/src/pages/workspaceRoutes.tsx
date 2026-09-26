/** The route tree under `/workspaces/:workspaceId`, shared by the app and its tests. */

import { Navigate, Route } from 'react-router-dom';

import { RunDetail } from './RunDetail';
import { WorkspaceLayout } from './WorkspaceLayout';
import { ConfigVersionsPage } from './workspace/ConfigVersionsPage';
import { OverviewPage } from './workspace/OverviewPage';
import { RunsPage } from './workspace/RunsPage';
import { VariablesPage } from './workspace/VariablesPage';
import { DeletionSettings } from './workspace/settings/DeletionSettings';
import { GeneralSettings } from './workspace/settings/GeneralSettings';
import { RunRoleSettings } from './workspace/settings/RunRoleSettings';

/** The workspace pages, nested under the workspace shell. */
export function workspaceRoutes(): React.ReactElement {
  return (
    <Route path="/workspaces/:workspaceId" element={<WorkspaceLayout />}>
      <Route index element={<OverviewPage />} />
      <Route path="runs" element={<RunsPage />} />
      <Route path="runs/:runId" element={<RunDetail />} />
      <Route path="configuration-versions" element={<ConfigVersionsPage />} />
      <Route path="variables" element={<VariablesPage />} />
      <Route path="settings" element={<Navigate to="general" replace />} />
      <Route path="settings/general" element={<GeneralSettings />} />
      <Route path="settings/run-role" element={<RunRoleSettings />} />
      <Route path="settings/deletion" element={<DeletionSettings />} />
    </Route>
  );
}
