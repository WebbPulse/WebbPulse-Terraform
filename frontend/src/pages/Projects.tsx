/**
 * The projects list, as HCP Terraform's: every project with the workspaces in
 * it side by side and each one's latest run, and the form that adds a project.
 */

import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type Project,
  type ProjectCreate,
  type ProjectList,
  type WorkspaceList,
  type WorkspaceListItem,
} from '../api';
import {
  Button,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
} from '../components';
import { RunStatusCell } from './workspaceListing';
import {
  browsableProjects,
  groupByProject,
  matchingName,
  withParam,
} from './workspaceListingRules';
import { PROJECTS_KEY, WORKSPACES_KEY } from './Workspaces';

/** The longest project name the API accepts. */
const PROJECT_NAME_MAX = 40;

/** The projects page. */
export function Projects(): React.ReactElement {
  const auth = useQueryAuth();
  const [params, setParams] = useSearchParams();
  const search = params.get('q') ?? '';
  const [creating, setCreating] = useState(false);
  const projects = usePolledQuery<ProjectList>(
    ({ signal }) => api.listProjects({ signal }),
    { intervalMs: 30_000, queryKey: PROJECTS_KEY, auth }
  );
  const workspaces = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }, { sort: 'name' }),
    { intervalMs: 30_000, queryKey: [WORKSPACES_KEY, 'name'], auth }
  );
  const listed = browsableProjects(projects.data?.items ?? []);
  const shown = matchingName(listed, search);
  const groups = new Map(
    groupByProject(workspaces.data?.items ?? [], shown).map((group) => [
      group.project_id,
      group.workspaces,
    ])
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Projects"
        description="Projects group the workspaces that ship together, such as one service's staging and production."
        meta={
          projects.isFetching && !projects.isLoading ? (
            <Spinner label="Refreshing projects" className="size-3.5" />
          ) : null
        }
        actions={
          creating ? null : (
            <Button
              variant="primary"
              onClick={() => {
                setCreating(true);
              }}
            >
              New project
            </Button>
          )
        }
      />
      {creating ? (
        <CreateProjectForm
          onDone={() => {
            setCreating(false);
          }}
        />
      ) : null}
      <ErrorNotice error={projects.error} />
      <ErrorNotice error={workspaces.error} />
      {projects.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading projects" className="size-4" />
          Loading projects
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <input
              type="search"
              aria-label="Filter projects by name"
              placeholder="Filter projects by name"
              value={search}
              onChange={(event) => {
                setParams(
                  (current) => withParam(current, 'q', event.target.value),
                  { replace: true }
                );
              }}
              className={`${INPUT_CLASS} mt-0! max-w-xs`}
            />
            <span className="text-xs text-text-faint">
              {shown.length} of {listed.length}
            </span>
          </div>
          {shown.length === 0 ? (
            <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
              {search.trim() === ''
                ? 'No projects yet.'
                : 'No projects match that name.'}
            </p>
          ) : (
            <Table label="Projects">
              <thead>
                <tr>
                  <Th className="w-1/3">Project</Th>
                  <Th>Workspaces and their latest runs</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((project) => (
                  <ProjectRow
                    key={project.project_id}
                    project={project}
                    workspaces={groups.get(project.project_id) ?? []}
                  />
                ))}
              </tbody>
            </Table>
          )}
        </div>
      )}
    </div>
  );
}

/** One project, with each of its workspaces and the state of its newest run. */
function ProjectRow({
  project,
  workspaces,
}: {
  project: Project;
  workspaces: readonly WorkspaceListItem[];
}): React.ReactElement {
  return (
    <Tr>
      <Td className="w-1/3 align-top">
        <Link
          to={`/projects/${project.project_id}`}
          className="font-medium text-text-strong hover:text-accent hover:underline"
        >
          {project.name}
        </Link>
        <p className="text-xs text-text-faint">
          {project.workspace_count === 1
            ? '1 workspace'
            : `${String(project.workspace_count ?? 0)} workspaces`}
        </p>
        {(project.description ?? '') === '' ? null : (
          <p className="line-clamp-2 text-xs text-text-faint">
            {project.description}
          </p>
        )}
      </Td>
      <Td>
        {workspaces.length === 0 ? (
          <span className="text-xs text-text-faint">No workspaces yet</span>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {workspaces.map((workspace) => (
              <li
                key={workspace.workspace_id}
                className="flex items-center gap-2 rounded-md border border-line bg-raised px-2 py-1"
              >
                <Link
                  to={`/workspaces/${workspace.workspace_id}`}
                  className="text-xs font-medium text-text-strong hover:text-accent hover:underline"
                >
                  {workspace.name}
                </Link>
                <RunStatusCell workspace={workspace} />
              </li>
            ))}
          </ul>
        )}
      </Td>
    </Tr>
  );
}

/** The inline form that adds a project. */
function CreateProjectForm({
  onDone,
}: {
  onDone: () => void;
}): React.ReactElement {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (body: ProjectCreate) => api.createProject(body),
    PROJECTS_KEY
  );
  const submit = async (): Promise<void> => {
    const body: ProjectCreate = { name: name.trim() };
    if (description.trim() !== '') {
      body.description = description.trim();
    }
    try {
      await mutate(body);
      onDone();
    } catch {
      return;
    }
  };
  return (
    <form
      aria-label="Create a project"
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field
        label="Project name"
        hint="Letters, digits, spaces, hyphens and underscores. Unique ignoring case."
      >
        {(control) => (
          <input
            {...control}
            autoFocus
            value={name}
            maxLength={PROJECT_NAME_MAX}
            onChange={(event) => {
              setName(event.target.value);
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <Field label="Description" hint="Optional.">
        {(control) => (
          <input
            {...control}
            value={description}
            maxLength={256}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <ErrorNotice error={error} />
      <div className="flex items-center gap-2">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Creating the project"
          disabled={name.trim() === ''}
        >
          Create project
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
