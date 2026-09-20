/** Everything a person needs to let runs into their AWS account. */

import { useEffect, useId, useState } from 'react';
import {
  invalidateQueries,
  useMutationWithRefetch,
} from '@webbpulse/api-client/react';

import {
  SNIPPET_FORMATS,
  api,
  isConnected,
  runRoleArnProblem,
  runRolePrefix,
  snippetFor,
  trustedPrincipals,
  type RunRoleCheck,
  type SnippetFormat,
  type Workspace,
} from '../../api';
import {
  Button,
  CodeBlock,
  CopyButton,
  ErrorNotice,
  SegmentedControl,
  formatDateTime,
} from '../../components';

/** Props for {@link ConnectAccountPanel}. */
export interface ConnectAccountPanelProps {
  workspace: Workspace;
  /** The refetch key the workspace read is registered under. */
  queryKey: string;
  /** Folds the role creation snippets behind a disclosure. */
  collapsible?: boolean;
}

/** The explanation, the role snippets, and the ARN form with its check. */
export function ConnectAccountPanel({
  workspace,
  queryKey,
  collapsible = false,
}: ConnectAccountPanelProps): React.ReactElement {
  const { run_role_setup: setup } = workspace;
  return (
    <div className="space-y-5">
      <p className="max-w-prose text-sm text-surface-300">
        Runs assume an IAM role in your AWS account to read and write your
        infrastructure. Create the role below with a trust policy that names the
        runner and this workspace's external id, then save its ARN here.
      </p>
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
        <ValueRow label="Role name" value={setup.role_name} />
        {trustedPrincipals(setup).map((arn, index) => (
          <ValueRow
            key={arn}
            label={index === 0 ? 'Trusted principals' : ''}
            value={arn}
          />
        ))}
        <ValueRow label="External id" value={setup.external_id} />
      </dl>
      {collapsible ? (
        <details className="group rounded-md border border-surface-700">
          <summary className="cursor-pointer px-3 py-2 text-sm text-surface-200 select-none hover:text-surface-50">
            Role creation snippets
          </summary>
          <div className="border-t border-surface-700 p-3">
            <RoleSnippets workspace={workspace} />
          </div>
        </details>
      ) : (
        <RoleSnippets workspace={workspace} />
      )}
      <RoleArnForm workspace={workspace} queryKey={queryKey} />
    </div>
  );
}

/** One label and copyable monospace value. */
function ValueRow({
  label,
  value,
}: {
  label: string;
  value: string;
}): React.ReactElement {
  return (
    <>
      <dt className="text-surface-400 sm:py-1">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2">
        <code className="min-w-0 truncate rounded bg-surface-900 px-1.5 py-1 font-mono text-xs text-surface-100">
          {value}
        </code>
        <CopyButton value={value} subject={value} />
      </dd>
    </>
  );
}

/** The segmented control over the four ways to create the role. */
function RoleSnippets({
  workspace,
}: {
  workspace: Workspace;
}): React.ReactElement {
  const [format, setFormat] = useState<SnippetFormat>('terraform');
  const { run_role_setup: setup } = workspace;
  const label =
    SNIPPET_FORMATS.find((entry) => entry.id === format)?.label ?? format;
  return (
    <div className="space-y-3">
      <SegmentedControl
        label="Role creation format"
        segments={SNIPPET_FORMATS}
        value={format}
        onChange={setFormat}
      />
      <CodeBlock
        code={snippetFor(format, setup)}
        subject={`${label} snippet`}
      />
      <p className="text-xs text-surface-400">
        The snippets attach AdministratorAccess so a first run has what it
        needs. Attach a narrower policy instead when the configuration needs
        less.
      </p>
    </div>
  );
}

/** The ARN input with its save and connection check. */
function RoleArnForm({
  workspace,
  queryKey,
}: {
  workspace: Workspace;
  queryKey: string;
}): React.ReactElement {
  const { run_role_setup: setup } = workspace;
  const [arn, setArn] = useState(workspace.run_role_arn ?? '');
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [result, setResult] = useState<RunRoleCheck | null>(null);
  const helpId = useId();
  const problemId = useId();

  useEffect(() => {
    setArn(workspace.run_role_arn ?? '');
    setProblem(null);
  }, [workspace.run_role_arn]);

  const save = useMutationWithRefetch(
    (value: string) =>
      api.updateWorkspace(workspace.workspace_id, { run_role_arn: value }),
    queryKey
  );

  const dirty = arn.trim() !== (workspace.run_role_arn ?? '');
  const unsaved = (workspace.run_role_arn ?? null) === null;

  const submit = async (): Promise<void> => {
    setSaved(false);
    const value = arn.trim();
    const why = runRoleArnProblem(value, setup);
    if (why !== null) {
      setProblem(why);
      return;
    }
    setProblem(null);
    try {
      await save.mutate(value);
      setResult(null);
      setSaved(true);
    } catch {
      return;
    }
  };

  const check = async (): Promise<void> => {
    setChecking(true);
    setCheckError(null);
    setSaved(false);
    try {
      const outcome = await api.checkRunRole(workspace.workspace_id);
      setResult(outcome);
      invalidateQueries([queryKey]);
    } catch (thrown) {
      setCheckError(thrown);
    } finally {
      setChecking(false);
    }
  };

  return (
    <form
      aria-label="Run role"
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="block text-sm">
        <span className="text-surface-300">Role ARN</span>
        <input
          value={arn}
          aria-invalid={problem !== null}
          aria-describedby={
            problem === null ? helpId : `${helpId} ${problemId}`
          }
          placeholder={`arn:aws:iam::123456789012:role/${setup.role_name}`}
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => {
            setArn(event.target.value);
            setProblem(null);
            setSaved(false);
          }}
          className="mt-1 h-8 w-full rounded-md border border-surface-600 bg-surface-900 px-2.5 font-mono text-xs text-surface-100 placeholder:text-surface-500 focus-visible:border-brand-400 focus-visible:ring-1 focus-visible:ring-brand-400 focus-visible:outline-none aria-[invalid=true]:border-rose-500"
        />
      </label>
      <p id={helpId} className="text-xs text-surface-400">
        The runner only assumes roles whose name starts with{' '}
        <code className="font-mono text-surface-200">
          {runRolePrefix(setup.role_name)}
        </code>
        , so keep that prefix if you rename the role.
      </p>
      {problem === null ? null : (
        <p id={problemId} role="alert" className="text-sm text-rose-300">
          {problem}
        </p>
      )}
      <ErrorNotice error={save.error} />
      <ErrorNotice error={checkError} />
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="submit"
          variant={dirty || unsaved ? 'primary' : 'secondary'}
          busy={save.isMutating}
          busyLabel="Saving the role ARN"
          disabled={arn.trim() === ''}
        >
          Save
        </Button>
        <Button
          variant={dirty || unsaved ? 'secondary' : 'primary'}
          busy={checking}
          busyLabel="Checking the connection"
          disabled={unsaved || dirty}
          title={
            dirty
              ? 'Save the ARN before checking it.'
              : unsaved
                ? 'Save a role ARN first.'
                : undefined
          }
          onClick={() => {
            void check();
          }}
        >
          Check connection
        </Button>
        {saved ? (
          <span role="status" className="text-sm text-emerald-300">
            Saved. Now check the connection.
          </span>
        ) : null}
      </div>
      <ConnectionStatus workspace={workspace} result={result} />
    </form>
  );
}

/** The outcome of the last check, fresh from the button or as persisted. */
function ConnectionStatus({
  workspace,
  result,
}: {
  workspace: Workspace;
  result: RunRoleCheck | null;
}): React.ReactElement | null {
  const checkedAt = workspace.run_role_checked_at ?? null;
  if (result !== null) {
    const error = result.error ?? null;
    return result.connected ? (
      <StatusLine tone="ok" testValue="connected">
        Connected to account{' '}
        <code className="font-mono">{result.account_id ?? 'unknown'}</code>.
      </StatusLine>
    ) : (
      <StatusLine tone="bad" testValue="failed">
        Could not assume the role
        {error === null ? '.' : `: ${error}`}
      </StatusLine>
    );
  }
  if ((workspace.run_role_arn ?? null) === null) {
    return null;
  }
  if (isConnected(workspace)) {
    return (
      <StatusLine tone="ok" testValue="connected">
        Connected to account{' '}
        <code className="font-mono">{workspace.run_role_account_id}</code>
        {checkedAt === null ? '.' : `, checked ${formatDateTime(checkedAt)}.`}
      </StatusLine>
    );
  }
  if (checkedAt !== null) {
    return (
      <StatusLine tone="bad" testValue="failed">
        The last check, {formatDateTime(checkedAt)}, could not assume the role.
        Check again once the trust policy is in place.
      </StatusLine>
    );
  }
  return (
    <StatusLine tone="neutral" testValue="unchecked">
      Not checked yet.
    </StatusLine>
  );
}

/** One status sentence with a coloured dot. */
function StatusLine({
  tone,
  testValue,
  children,
}: {
  tone: 'ok' | 'bad' | 'neutral';
  testValue: string;
  children: React.ReactNode;
}): React.ReactElement {
  const dot = {
    ok: 'bg-emerald-400',
    bad: 'bg-rose-400',
    neutral: 'bg-surface-500',
  }[tone];
  const text = {
    ok: 'text-emerald-200',
    bad: 'text-rose-200',
    neutral: 'text-surface-300',
  }[tone];
  return (
    <p
      role="status"
      data-testid="run-role-status"
      data-connection={testValue}
      className={`flex items-start gap-2 text-sm ${text}`}
    >
      <span
        aria-hidden="true"
        className={`mt-1.5 inline-block size-2 shrink-0 rounded-full ${dot}`}
      />
      <span>{children}</span>
    </p>
  );
}
