/** Everything a person needs to let runs into their AWS account. */

import { useEffect, useState } from 'react';
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
  Field,
  INPUT_CLASS,
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
      <p className="max-w-prose text-sm text-text-muted">
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
        <details className="group rounded-md border border-line">
          <summary className="cursor-pointer px-3 py-2 text-sm text-text select-none hover:text-text-strong">
            Role creation snippets
          </summary>
          <div className="border-t border-line p-3">
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
      <dt className="text-text-faint sm:py-1">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2">
        <code className="min-w-0 truncate rounded bg-bg px-1.5 py-1 font-mono text-xs text-text">
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
      <p className="text-xs text-text-faint">
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
      <Field
        label="Role ARN"
        error={problem}
        hint={
          <>
            The runner only assumes roles whose name starts with{' '}
            <code className="font-mono text-text">
              {runRolePrefix(setup.role_name)}
            </code>
            , so keep that prefix if you rename the role.
          </>
        }
      >
        {(control) => (
          <input
            {...control}
            value={arn}
            placeholder={`arn:aws:iam::123456789012:role/${setup.role_name}`}
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => {
              setArn(event.target.value);
              setProblem(null);
              setSaved(false);
            }}
            className={`${INPUT_CLASS} font-mono text-xs`}
          />
        )}
      </Field>
      <ErrorNotice error={save.error} />
      <ErrorNotice error={checkError} />
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="submit"
          variant={
            dirty || workspace.run_role_arn === null ? 'primary' : 'secondary'
          }
          busy={save.isMutating}
          busyLabel="Saving the role ARN"
          disabled={arn.trim() === ''}
        >
          Save
        </Button>
        <Button
          variant={
            dirty || workspace.run_role_arn === null ? 'secondary' : 'primary'
          }
          busy={checking}
          busyLabel="Checking the connection"
          disabled={workspace.run_role_arn === null || dirty}
          title={
            dirty
              ? 'Save the ARN before checking it.'
              : workspace.run_role_arn === null
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
  if (result !== null) {
    return result.connected ? (
      <StatusLine tone="ok" testValue="connected">
        Connected to account{' '}
        <code className="font-mono">{result.account_id ?? 'unknown'}</code>.
      </StatusLine>
    ) : (
      <StatusLine tone="bad" testValue="failed">
        Could not assume the role
        {result.error === null ? '.' : `: ${result.error}`}
      </StatusLine>
    );
  }
  if (workspace.run_role_arn === null) {
    return null;
  }
  if (isConnected(workspace)) {
    return (
      <StatusLine tone="ok" testValue="connected">
        Connected to account{' '}
        <code className="font-mono">{workspace.run_role_account_id}</code>
        {workspace.run_role_checked_at === null
          ? '.'
          : `, checked ${formatDateTime(workspace.run_role_checked_at)}.`}
      </StatusLine>
    );
  }
  if (workspace.run_role_checked_at !== null) {
    return (
      <StatusLine tone="bad" testValue="failed">
        The last check, {formatDateTime(workspace.run_role_checked_at)}, could
        not assume the role. Check again once the trust policy is in place.
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
    neutral: 'text-text-muted',
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
