/**
 * The page that creates a workspace, in HCP Terraform's steps.
 *
 * The workflow comes first. Version control then picks a repository before
 * the settings, and the CLI and API workflows go straight to the settings.
 */

import { useId, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type WorkspaceCreate } from '../api';
import {
  Button,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  buttonClass,
  useIsAdmin,
} from '../components';
import { WORKSPACES_KEY } from './Workspaces';
import {
  NAME_MAX_LENGTH,
  missingForCreate,
  nameFromRepository,
} from './newWorkspaceRules';
import { EngineFields, type EngineChoice } from './workspace/EngineFields';
import { defaultEngineVersion } from './workspace/engineVersions';
import { RepositoryList } from './workspace/vcs/RepositoryPicker';
import {
  useInstalledRepositories,
  type InstalledRepository,
} from './workspace/vcs/useInstalledRepositories';
import { VcsFields } from './workspace/vcs/VcsFields';
import {
  EMPTY_VCS_SETTINGS,
  WORKFLOWS,
  createBody,
  type VcsSettings,
  type Workflow,
} from './workspace/vcs/vcsSettings';

/** One step of the flow. */
type StepId = 'workflow' | 'repository' | 'settings';

/** The steps a workflow walks through, with their labels. */
function stepsFor(
  workflow: Workflow | null
): readonly { id: StepId; label: string }[] {
  const first = { id: 'workflow' as const, label: 'Choose your workflow' };
  const last = { id: 'settings' as const, label: 'Configure settings' };
  return workflow === 'vcs'
    ? [first, { id: 'repository', label: 'Connect to a repository' }, last]
    : [first, last];
}

/** The new workspace page. */
export function NewWorkspace(): React.ReactElement {
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const repositories = useInstalledRepositories(isAdmin);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [step, setStep] = useState<StepId>('workflow');
  const [repository, setRepository] = useState<InstalledRepository | null>(
    null
  );
  const [name, setName] = useState('');
  const [nameEdited, setNameEdited] = useState(false);
  const [description, setDescription] = useState('');
  const [engine, setEngine] = useState<EngineChoice>({
    engine: 'terraform',
    version: defaultEngineVersion('terraform'),
  });
  const [vcs, setVcs] = useState<VcsSettings>(EMPTY_VCS_SETTINGS);
  const [advanced, setAdvanced] = useState(false);
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (body: WorkspaceCreate) => api.createWorkspace(body),
    WORKSPACES_KEY
  );

  const steps = stepsFor(workflow);

  const chooseWorkflow = (next: Workflow): void => {
    setWorkflow(next);
    setStep(next === 'vcs' ? 'repository' : 'settings');
  };

  const chooseRepository = (picked: InstalledRepository): void => {
    setRepository(picked);
    setVcs((current) => ({ ...current, branch: '' }));
    if (!nameEdited) {
      setName(nameFromRepository(picked.name));
    }
    setStep('settings');
  };

  const back = (): void => {
    if (step === 'settings' && workflow === 'vcs') {
      setStep('repository');
      return;
    }
    setStep('workflow');
  };

  const submit = async (): Promise<void> => {
    if (workflow === null) {
      return;
    }
    const base: WorkspaceCreate = {
      name: name.trim(),
      engine: engine.engine,
      engine_version: engine.version.trim().replace(/^v/, ''),
    };
    if (description.trim() !== '') {
      base.description = description.trim();
    }
    try {
      const created = await mutate(
        createBody(base, workflow, repository?.full_name ?? null, vcs)
      );
      void navigate(`/workspaces/${created.workspace_id}`);
    } catch {
      return;
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        crumbs={[{ label: 'Workspaces', to: '/workspaces' }]}
        title="Create a new workspace"
        actions={
          <Link to="/workspaces" className={buttonClass('ghost')}>
            Cancel
          </Link>
        }
      />
      <StepIndicator
        steps={steps}
        current={step}
        onJump={(target) => {
          setStep(target);
        }}
      />
      {step === 'workflow' ? (
        <WorkflowStep onChoose={chooseWorkflow} />
      ) : step === 'repository' ? (
        <StepSection
          title="Connect to a repository"
          description="Choose the repository this workspace plans and applies from. Pushes to its branch start runs, and pull requests get plan-only runs."
          footer={
            <Button variant="secondary" onClick={back}>
              Back
            </Button>
          }
        >
          <RepositoryList
            isAdmin={isAdmin}
            source={repositories}
            value={repository?.full_name ?? null}
            onChange={chooseRepository}
          />
        </StepSection>
      ) : (
        <SettingsStep
          workflow={workflow ?? 'cli'}
          repository={repository}
          name={name}
          onName={(value) => {
            setName(value);
            setNameEdited(true);
          }}
          description={description}
          onDescription={setDescription}
          engine={engine}
          onEngine={setEngine}
          vcs={vcs}
          onVcs={setVcs}
          advanced={advanced}
          onAdvanced={setAdvanced}
          error={error}
          busy={isMutating}
          onBack={back}
          onChangeRepository={() => {
            setStep('repository');
          }}
          onSubmit={() => {
            void submit();
          }}
        />
      )}
    </div>
  );
}

/**
 * The numbered steps across the top, as HCP Terraform draws them.
 *
 * A finished step is a button back to itself, so a choice can be revisited
 * without walking back one step at a time.
 */
function StepIndicator({
  steps,
  current,
  onJump,
}: {
  steps: readonly { id: StepId; label: string }[];
  current: StepId;
  onJump: (step: StepId) => void;
}): React.ReactElement {
  const currentIndex = steps.findIndex((step) => step.id === current);
  return (
    <nav aria-label="Steps">
      <ol className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        {steps.map((step, index) => {
          const done = index < currentIndex;
          const active = index === currentIndex;
          const marker = (
            <span
              aria-hidden="true"
              className={`inline-flex size-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold ${
                active
                  ? 'border-accent bg-accent text-accent-contrast'
                  : done
                    ? 'border-accent-line bg-accent-soft text-accent'
                    : 'border-line-strong text-text-faint'
              }`}
            >
              {done ? (
                <svg viewBox="0 0 16 16" className="size-3.5" fill="none">
                  <path
                    d="m3.5 8.5 3 3 6-7"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              ) : (
                index + 1
              )}
            </span>
          );
          const label = (
            <span
              className={
                active
                  ? 'font-medium text-text-strong'
                  : done
                    ? 'text-text'
                    : 'text-text-faint'
              }
            >
              {step.label}
            </span>
          );
          return (
            <li key={step.id} className="flex items-center gap-3">
              {index === 0 ? null : (
                <span aria-hidden="true" className="h-px w-6 bg-line-strong" />
              )}
              {done ? (
                <button
                  type="button"
                  onClick={() => {
                    onJump(step.id);
                  }}
                  className="flex items-center gap-2 rounded-md hover:[&>span:last-child]:text-accent"
                >
                  {marker}
                  {label}
                  <span className="sr-only">, done</span>
                </button>
              ) : (
                <span
                  className="flex items-center gap-2"
                  aria-current={active ? 'step' : undefined}
                >
                  {marker}
                  {label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** A step's panel: its heading, one line of context, the body and its buttons. */
function StepSection({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}): React.ReactElement {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <div>
        <h2 id={headingId} className="text-sm font-semibold text-text-strong">
          {title}
        </h2>
        {description === undefined ? null : (
          <p className="mt-1 max-w-prose text-sm text-text-muted">
            {description}
          </p>
        )}
      </div>
      {children}
      {footer === undefined ? null : (
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          {footer}
        </div>
      )}
    </section>
  );
}

/** The glyph each workflow tile carries. */
const WORKFLOW_ICONS: Record<Workflow, ReactNode> = {
  vcs: (
    <path
      d="M5 3.5v9M5 12.5a1.5 1.5 0 1 0 0 .01M5 3.5a1.5 1.5 0 1 0 0-.01M11 5.5a1.5 1.5 0 1 0 0-.01M11 7c0 2.5-6 2-6 4"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  cli: (
    <path
      d="m3.5 5 3 3-3 3M8.5 11.5h4"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  api: (
    <path
      d="M5.5 3C4 3 4 4 4 5.5S3.5 8 2.5 8c1 0 1.5 1 1.5 2.5S4 13 5.5 13M10.5 3C12 3 12 4 12 5.5S12.5 8 13.5 8c-1 0-1.5 1-1.5 2.5S12 13 10.5 13"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
};

/** Step one: three wide tiles, version control first, each advancing on a click. */
function WorkflowStep({
  onChoose,
}: {
  onChoose: (workflow: Workflow) => void;
}): React.ReactElement {
  return (
    <StepSection
      title="Choose your workflow"
      description="How runs start in this workspace. It can be changed later in the workspace settings."
    >
      <ul className="space-y-3">
        {WORKFLOWS.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              aria-describedby={`workflow-${item.id}-description`}
              onClick={() => {
                onChoose(item.id);
              }}
              className="group flex w-full items-center gap-4 rounded-lg border border-line bg-panel px-4 py-4 text-left transition-colors hover:border-accent-line hover:bg-raised focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
            >
              <span
                aria-hidden="true"
                className="inline-flex size-10 shrink-0 items-center justify-center rounded-md border border-line bg-raised text-text-muted group-hover:border-accent-line group-hover:text-accent"
              >
                <svg viewBox="0 0 16 16" className="size-5" fill="none">
                  {WORKFLOW_ICONS[item.id]}
                </svg>
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold text-text-strong">
                  {item.label}
                </span>
                <span
                  id={`workflow-${item.id}-description`}
                  className="mt-0.5 block text-sm text-text-muted"
                >
                  {item.description}
                </span>
              </span>
              <svg
                viewBox="0 0 16 16"
                aria-hidden="true"
                className="size-4 shrink-0 text-text-faint group-hover:text-accent"
                fill="none"
              >
                <path
                  d="m6 3 5 5-5 5"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </li>
        ))}
      </ul>
    </StepSection>
  );
}

/** Props for {@link SettingsStep}. */
interface SettingsStepProps {
  workflow: Workflow;
  repository: InstalledRepository | null;
  name: string;
  onName: (value: string) => void;
  description: string;
  onDescription: (value: string) => void;
  engine: EngineChoice;
  onEngine: (value: EngineChoice) => void;
  vcs: VcsSettings;
  onVcs: (value: VcsSettings) => void;
  advanced: boolean;
  onAdvanced: (open: boolean) => void;
  error: unknown;
  busy: boolean;
  onBack: () => void;
  onChangeRepository: () => void;
  onSubmit: () => void;
}

/** The last step: name and description, with the rest under advanced options. */
function SettingsStep({
  workflow,
  repository,
  name,
  onName,
  description,
  onDescription,
  engine,
  onEngine,
  vcs,
  onVcs,
  advanced,
  onAdvanced,
  error,
  busy,
  onBack,
  onChangeRepository,
  onSubmit,
}: SettingsStepProps): React.ReactElement {
  const advancedId = useId();
  const hintId = useId();
  const missing = missingForCreate({
    name,
    engine,
    workflow,
    repository: repository?.full_name ?? null,
  });
  const chosen = WORKFLOWS.find((item) => item.id === workflow);
  return (
    <form
      aria-label="Create a workspace"
      onSubmit={(event) => {
        event.preventDefault();
        if (missing === null) {
          onSubmit();
        }
      }}
    >
      <StepSection
        title="Configure settings"
        footer={
          <>
            <Button variant="secondary" onClick={onBack}>
              Back
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={busy}
              busyLabel="Creating the workspace"
              disabled={missing !== null}
              aria-describedby={missing === null ? undefined : hintId}
            >
              Create workspace
            </Button>
            {missing === null ? null : (
              <p id={hintId} className="text-xs text-text-muted">
                {missing}
              </p>
            )}
          </>
        }
      >
        <dl className="grid gap-x-6 gap-y-2 rounded-lg border border-line bg-panel px-4 py-3 text-sm sm:grid-cols-[auto_1fr]">
          <dt className="text-text-muted">Workflow</dt>
          <dd className="text-text-strong">{chosen?.label}</dd>
          {repository === null ? null : (
            <>
              <dt className="text-text-muted">Repository</dt>
              <dd className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="font-mono text-text-strong">
                  {repository.full_name}
                </span>
                <button
                  type="button"
                  onClick={onChangeRepository}
                  className="text-xs text-accent underline-offset-2 hover:underline"
                >
                  Change repository
                </button>
              </dd>
            </>
          )}
        </dl>
        <Field
          label="Workspace name"
          hint={
            repository === null
              ? 'Letters, digits, dots, underscores and hyphens.'
              : 'Taken from the repository name. Letters, digits, dots, underscores and hyphens.'
          }
        >
          {(control) => (
            <input
              {...control}
              autoFocus
              value={name}
              maxLength={NAME_MAX_LENGTH}
              onChange={(event) => {
                onName(event.target.value);
              }}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field label="Description" hint="Optional.">
          {(control) => (
            <textarea
              {...control}
              rows={2}
              value={description}
              onChange={(event) => {
                onDescription(event.target.value);
              }}
              className={`${INPUT_CLASS} h-auto py-1.5`}
            />
          )}
        </Field>
        <div className="rounded-lg border border-line bg-panel">
          <button
            type="button"
            aria-expanded={advanced}
            aria-controls={advancedId}
            onClick={() => {
              onAdvanced(!advanced);
            }}
            className="flex w-full items-center gap-2 rounded-lg px-4 py-3 text-left text-sm font-medium text-text-strong hover:bg-raised/60 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
          >
            <svg
              viewBox="0 0 16 16"
              aria-hidden="true"
              className={`size-3.5 shrink-0 text-text-faint transition-transform ${advanced ? 'rotate-90' : ''}`}
              fill="none"
            >
              <path
                d="m6 3 5 5-5 5"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Advanced options
            <span className="ml-auto text-xs font-normal text-text-faint">
              {workflow === 'vcs'
                ? 'Working directory, branch, triggers, engine'
                : 'Engine and version'}
            </span>
          </button>
          {advanced ? (
            <div
              id={advancedId}
              className="space-y-5 border-t border-line px-4 py-4"
            >
              <EngineFields value={engine} onChange={onEngine} />
              {workflow === 'vcs' ? (
                <VcsFields
                  value={vcs}
                  onChange={onVcs}
                  defaultBranch={repository?.default_branch ?? null}
                />
              ) : null}
            </div>
          ) : null}
        </div>
        <ErrorNotice error={error} />
      </StepSection>
    </form>
  );
}
