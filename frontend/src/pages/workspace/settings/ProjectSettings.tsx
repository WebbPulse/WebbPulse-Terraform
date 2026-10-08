/** Moves a workspace to another project, which changes nothing about its runs or state. */

import { useEffect, useState } from 'react';
import {
  invalidateQueries,
  useMutationWithRefetch,
} from '@webbpulse/api-client/react';

import { api, DEFAULT_PROJECT_ID, type Workspace } from '../../../api';
import { Button, ErrorNotice } from '../../../components';
import { ProjectPicker } from '../../ProjectPicker';
import { PROJECTS_KEY } from '../../Workspaces';

/** The project form on the general settings page. */
export function ProjectSettings({
  workspace,
  queryKey,
}: {
  workspace: Workspace;
  queryKey: string;
}): React.ReactElement {
  const current = workspace.project_id ?? DEFAULT_PROJECT_ID;
  const [projectId, setProjectId] = useState(current);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setProjectId(current);
  }, [current]);
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (target: string) =>
      api.updateWorkspace(workspace.workspace_id, { project_id: target }),
    queryKey
  );
  const submit = async (): Promise<void> => {
    setSaved(false);
    try {
      await mutate(projectId);
      invalidateQueries(PROJECTS_KEY);
      setSaved(true);
    } catch {
      return;
    }
  };
  return (
    <form
      aria-label="Workspace project"
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <ProjectPicker
        value={projectId}
        onChange={(value) => {
          setSaved(false);
          setProjectId(value);
        }}
        hint="Moving a workspace changes only how it is grouped. Its runs, variables and state stay as they are."
      />
      <ErrorNotice error={error} />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Moving the workspace"
          disabled={projectId === current}
        >
          Move workspace
        </Button>
        {saved ? (
          <span role="status" className="text-xs text-text-muted">
            Moved.
          </span>
        ) : null}
      </div>
    </form>
  );
}
