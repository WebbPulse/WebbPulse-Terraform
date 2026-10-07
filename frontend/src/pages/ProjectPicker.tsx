/** The project select a workspace's create form and settings share. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type ProjectList } from '../api';
import { Field, INPUT_CLASS } from '../components';
import { PROJECTS_KEY } from './Workspaces';

/** Props for {@link ProjectPicker}. */
export interface ProjectPickerProps {
  value: string;
  onChange: (projectId: string) => void;
  hint?: string;
  disabled?: boolean;
}

/**
 * A select over every project, the default first. It renders nothing until the
 * projects load, or when they cannot, so the workspace stays in the project it
 * is in and the rest of the form still works.
 */
export function ProjectPicker({
  value,
  onChange,
  hint = 'The project groups this workspace with the others it ships alongside.',
  disabled = false,
}: ProjectPickerProps): React.ReactElement | null {
  const auth = useQueryAuth();
  const projects = usePolledQuery<ProjectList>(
    ({ signal }) => api.listProjects({ signal }),
    { intervalMs: 60_000, queryKey: PROJECTS_KEY, auth }
  );
  const items = projects.data?.items ?? [];
  if (items.length === 0) {
    return null;
  }
  return (
    <Field label="Project" hint={hint}>
      {(control) => (
        <select
          {...control}
          value={value}
          disabled={disabled}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          className={INPUT_CLASS}
        >
          {items.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
