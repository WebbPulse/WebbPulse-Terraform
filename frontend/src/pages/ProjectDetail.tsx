/**
 * One project: its workspaces, searchable and sortable, its recent runs across
 * them, and for a project other than the default, its name and its removal.
 */

import { useEffect, useState } from 'react';
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom';
import {
  invalidateQueries,
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type Project,
  type RunList as RunPage,
  type WorkspaceList,
} from '../api';
import {
  Button,
  buttonClass,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  RunList,
  Spinner,
} from '../components';
import { NameSearch, SortSelect, WorkspaceRows } from './workspaceListing';
import {
  DEFAULT_WORKSPACE_SORT,
  matchingName,
  readSort,
  withParam,
} from './workspaceListingRules';
import { PROJECTS_KEY, WORKSPACES_KEY } from './Workspaces';

/** How many runs the recent runs list shows. */
const RECENT_RUNS = 20;

/** The project page. */
export function ProjectDetail(): React.ReactElement {
  const { projectId = '' } = useParams();
  const auth = useQueryAuth();
  const [params, setParams] = useSearchParams();
  const sort = readSort(params.get('sort'));
  const search = params.get('q') ?? '';
  const projectKey = `project:${projectId}`;
  const project = usePolledQuery<Project>(
    ({ signal }) => api.getProject(projectId, { signal }),
    { intervalMs: 60_000, queryKey: projectKey, auth }
  );
  const workspaces = usePolledQuery<WorkspaceList>(
    ({ signal }) =>
      api.listWorkspaces({ signal }, { project_id: projectId, sort }),
    {
      intervalMs: 30_000,
      queryKey: [WORKSPACES_KEY, sort, projectId],
      auth,
    }
  );
  const runs = usePolledQuery<RunPage>(
    ({ signal }) =>
      api.listRuns({ project_id: projectId, limit: RECENT_RUNS }, { signal }),
    { intervalMs: 10_000, queryKey: `project-runs:${projectId}`, auth }
  );
  const update = (key: string, value: string, fallback = ''): void => {
    setParams((current) => withParam(current, key, value, fallback), {
      replace: true,
    });
  };
  const items = workspaces.data?.items ?? [];
  const shown = matchingName(items, search);
  const names = new Map(
    items.map((workspace) => [workspace.workspace_id, workspace.name])
  );
  const name = project.data?.name ?? 'Project';

  return (
    <div className="space-y-6">
      <PageHeader
        crumbs={[{ label: 'Projects', to: '/projects' }]}
        title={name}
        description={
          (project.data?.description ?? '') === ''
            ? undefined
            : project.data?.description
        }
        actions={
          <Link
            to={`/workspaces/new?project=${encodeURIComponent(projectId)}`}
            className={buttonClass('primary')}
          >
            New workspace
          </Link>
        }
      />
      <ErrorNotice error={project.error} />
      <section aria-label="Workspaces" className="space-y-3">
        <h2 className="text-sm font-semibold text-text-strong">Workspaces</h2>
        <ErrorNotice error={workspaces.error} />
        {workspaces.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading workspaces" className="size-4" />
            Loading workspaces
          </div>
        ) : items.length === 0 ? (
          <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
            No workspaces in this project yet. Create one here, or move one in
            from its general settings.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
                <NameSearch
                  value={search}
                  onChange={(value) => {
                    update('q', value);
                  }}
                />
                <SortSelect
                  value={sort}
                  onChange={(value) => {
                    update('sort', value, DEFAULT_WORKSPACE_SORT);
                  }}
                />
              </div>
              <span className="text-xs text-text-faint">
                {shown.length} of {items.length}
              </span>
            </div>
            {shown.length === 0 ? (
              <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
                No workspaces match that name.
              </p>
            ) : (
              <WorkspaceRows
                workspaces={shown}
                label={`Workspaces in ${name}`}
              />
            )}
          </>
        )}
      </section>
      <section aria-label="Recent runs" className="space-y-3">
        <h2 className="text-sm font-semibold text-text-strong">Recent runs</h2>
        <ErrorNotice error={runs.error} />
        {runs.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading runs" className="size-4" />
            Loading runs
          </div>
        ) : (
          <RunList
            runs={runs.data?.items ?? []}
            workspaceNames={names}
            emptyHint="Runs on this project's workspaces show here."
          />
        )}
      </section>
      {project.data === null || project.data.is_default === true ? null : (
        <ProjectSettings project={project.data} queryKey={projectKey} />
      )}
    </div>
  );
}

/** The rename form and the delete button of a project other than the default. */
function ProjectSettings({
  project,
  queryKey,
}: {
  project: Project;
  queryKey: string;
}): React.ReactElement {
  const navigate = useNavigate();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? '');
  const [confirming, setConfirming] = useState(false);
  useEffect(() => {
    setName(project.name);
    setDescription(project.description ?? '');
  }, [project.name, project.description]);
  const save = useMutationWithRefetch(
    () =>
      api.updateProject(project.project_id, {
        name: name.trim(),
        description: description.trim(),
      }),
    queryKey
  );
  const remove = useMutationWithRefetch(
    () => api.deleteProject(project.project_id),
    PROJECTS_KEY
  );
  const submit = async (): Promise<void> => {
    try {
      await save.mutate();
      invalidateQueries(PROJECTS_KEY);
    } catch {
      return;
    }
  };
  const destroy = async (): Promise<void> => {
    try {
      await remove.mutate();
      void navigate('/projects', { replace: true });
    } catch {
      return;
    }
  };
  const holdsWorkspaces = (project.workspace_count ?? 0) > 0;
  const unchanged =
    name.trim() === project.name &&
    description.trim() === (project.description ?? '');

  return (
    <section aria-label="Project settings" className="max-w-2xl space-y-3">
      <h2 className="text-sm font-semibold text-text-strong">
        Project settings
      </h2>
      <form
        aria-label="Project settings"
        className="space-y-4 rounded-lg border border-line bg-panel p-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field label="Project name">
          {(control) => (
            <input
              {...control}
              value={name}
              maxLength={40}
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
        <ErrorNotice error={save.error} />
        <Button
          type="submit"
          variant="primary"
          busy={save.isMutating}
          busyLabel="Saving the project"
          disabled={unchanged || name.trim() === ''}
        >
          Save project
        </Button>
      </form>
      <div className="space-y-3 rounded-lg border border-danger-line bg-panel p-4">
        <p className="text-sm text-text-muted">
          {holdsWorkspaces
            ? 'Move every workspace out of this project before deleting it. Nothing is moved for you.'
            : 'Deleting the project removes only the group. It holds no workspaces.'}
        </p>
        <ErrorNotice error={remove.error} />
        {confirming ? (
          <div className="flex items-center gap-2">
            <Button
              variant="danger"
              busy={remove.isMutating}
              busyLabel="Deleting the project"
              onClick={() => {
                void destroy();
              }}
            >
              Delete this project
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Keep it
            </Button>
          </div>
        ) : (
          <Button
            variant="danger"
            disabled={holdsWorkspaces}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Delete project
          </Button>
        )}
      </div>
    </section>
  );
}
