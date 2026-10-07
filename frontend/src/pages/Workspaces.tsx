/** The workspaces list, sortable, searchable and grouped by project, with the way to create one. */

import { Link, useSearchParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type Project,
  type ProjectList,
  type WorkspaceList,
  type WorkspaceListItem,
  type WorkspaceSort,
} from '../api';
import {
  EmptyState,
  ErrorNotice,
  buttonClass,
  PageHeader,
  SegmentedControl,
  Spinner,
} from '../components';
import { NameSearch, SortSelect, WorkspaceRows } from './workspaceListing';
import {
  DEFAULT_WORKSPACE_SORT,
  groupByProject,
  matchingName,
  readSort,
  withParam,
} from './workspaceListingRules';

/** The refetch key the list reads and the create form invalidates. */
export const WORKSPACES_KEY = 'workspaces';

/** The refetch key the project list reads and the project forms invalidate. */
export const PROJECTS_KEY = 'projects';

type Grouping = 'none' | 'project';

/** The workspaces list. */
export function Workspaces(): React.ReactElement {
  const auth = useQueryAuth();
  const [params, setParams] = useSearchParams();
  const sort = readSort(params.get('sort'));
  const search = params.get('q') ?? '';
  const grouping: Grouping =
    params.get('group') === 'project' ? 'project' : 'none';
  const query = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }, { sort }),
    { intervalMs: 30_000, queryKey: [WORKSPACES_KEY, sort], auth }
  );
  const projects = usePolledQuery<ProjectList>(
    ({ signal }) => api.listProjects({ signal }),
    {
      intervalMs: 60_000,
      queryKey: PROJECTS_KEY,
      auth,
      enabled: grouping === 'project',
    }
  );
  const update = (key: string, value: string, fallback = ''): void => {
    setParams((current) => withParam(current, key, value, fallback), {
      replace: true,
    });
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Workspaces"
        description="Each workspace holds one root module, its variables and its runs."
        meta={
          query.isFetching && !query.isLoading ? (
            <Spinner label="Refreshing workspaces" className="size-3.5" />
          ) : null
        }
        actions={
          <>
            <Link to="/projects" className={buttonClass('secondary')}>
              Projects
            </Link>
            <Link to="/workspaces/new" className={buttonClass('primary')}>
              New workspace
            </Link>
          </>
        }
      />
      <ErrorNotice error={query.error} />
      {grouping === 'project' ? <ErrorNotice error={projects.error} /> : null}
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading workspaces" className="size-4" />
          Loading workspaces
        </div>
      ) : (
        <WorkspaceListing
          workspaces={query.data?.items ?? []}
          projects={
            grouping === 'project' ? (projects.data?.items ?? null) : null
          }
          grouping={grouping}
          search={search}
          sort={sort}
          onSearch={(value) => {
            update('q', value);
          }}
          onSort={(value) => {
            update('sort', value, DEFAULT_WORKSPACE_SORT);
          }}
          onGroup={(value) => {
            update('group', value, 'none');
          }}
        />
      )}
    </div>
  );
}

/** The controls over the list, then the list flat or by project, or an invitation to create the first one. */
function WorkspaceListing({
  workspaces,
  projects,
  grouping,
  search,
  sort,
  onSearch,
  onSort,
  onGroup,
}: {
  workspaces: WorkspaceListItem[];
  projects: readonly Project[] | null;
  grouping: Grouping;
  search: string;
  sort: WorkspaceSort;
  onSearch: (value: string) => void;
  onSort: (value: WorkspaceSort) => void;
  onGroup: (value: Grouping) => void;
}): React.ReactElement {
  if (workspaces.length === 0) {
    return (
      <EmptyState
        title="No workspaces yet."
        hint="Connect a repository, or upload configuration from the CLI or the API."
        action={
          <Link to="/workspaces/new" className={buttonClass('primary')}>
            Create the first workspace
          </Link>
        }
      />
    );
  }
  const shown = matchingName(workspaces, search);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <NameSearch value={search} onChange={onSearch} />
          <SortSelect value={sort} onChange={onSort} />
          <SegmentedControl
            label="Group workspaces"
            value={grouping}
            onChange={onGroup}
            segments={[
              { id: 'none', label: 'All' },
              { id: 'project', label: 'By project' },
            ]}
          />
        </div>
        <span className="text-xs text-text-faint">
          {shown.length} of {workspaces.length}
        </span>
      </div>
      {shown.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
          No workspaces match that name.
        </p>
      ) : grouping === 'project' ? (
        projects === null ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading projects" className="size-4" />
            Loading projects
          </div>
        ) : (
          <ProjectGroups
            workspaces={shown}
            projects={projects}
            searching={search.trim() !== ''}
          />
        )
      ) : (
        <WorkspaceRows workspaces={shown} />
      )}
    </div>
  );
}

/** The list grouped by project, each project with its workspaces and their latest runs. */
function ProjectGroups({
  workspaces,
  projects,
  searching,
}: {
  workspaces: readonly WorkspaceListItem[];
  projects: readonly Project[];
  searching: boolean;
}): React.ReactElement {
  const groups = groupByProject(workspaces, projects).filter(
    (group) => !searching || group.workspaces.length > 0
  );
  return (
    <div className="space-y-6">
      {groups.map((group) => (
        <section
          key={group.project_id}
          aria-label={`Project ${group.name}`}
          className="space-y-2"
        >
          <div className="flex items-baseline gap-2">
            <h2 className="text-sm font-semibold text-text-strong">
              <Link
                to={`/projects/${group.project_id}`}
                className="hover:text-accent hover:underline"
              >
                {group.name}
              </Link>
            </h2>
            <span className="text-xs text-text-faint">
              {group.workspaces.length === 1
                ? '1 workspace'
                : `${String(group.workspaces.length)} workspaces`}
            </span>
          </div>
          {group.workspaces.length === 0 ? (
            <p className="rounded-lg border border-dashed border-line px-4 py-4 text-center text-sm text-text-faint">
              No workspaces in this project yet.
            </p>
          ) : (
            <WorkspaceRows
              workspaces={group.workspaces}
              label={`Workspaces in ${group.name}`}
            />
          )}
        </section>
      ))}
    </div>
  );
}
