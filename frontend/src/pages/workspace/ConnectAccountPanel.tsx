/** Everything a person needs to let runs into their AWS account. */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  invalidateQueries,
  useMutationWithRefetch,
} from '@webbpulse/api-client/react';

import {
  PERMISSIONS_CHOICES,
  SNIPPET_FORMATS,
  accountIdFromArn,
  accountIdProblem,
  accountStatus,
  api,
  normalizeAccountId,
  runRoleArnProblem,
  runRolePrefix,
  snippetFor,
  trustedPrincipals,
  type PendingRunRoleCheck,
  type RunRoleCheck,
  type RunRolePermissions,
  type RunRoleQuickSetup,
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
  runPath,
} from '../../components';
import { useOptionalWorkspace, type WorkspaceKeys } from '../workspaceContext';
import { latestUploaded } from './settings/latestUploaded';

/** Props for {@link ConnectAccountPanel}. */
export interface ConnectAccountPanelProps {
  workspace: Workspace;
  /** The live run role check the workspace frame shares, or null until it answers. */
  runRoleCheck: RunRoleCheck | null;
  /** The workspace frame's refetch keys, so a save or a check refreshes every reader. */
  keys: WorkspaceKeys;
}

/**
 * The saved role as a connected card, or the ways to connect one.
 *
 * With a role ARN saved the card leads, as HCP Terraform shows a configured
 * integration, and Change role opens the setup paths. Without one, AWS quick
 * setup comes first, the connection state under it, and the manual path is
 * folded away for anyone creating the role with their own tooling.
 */
export function ConnectAccountPanel({
  workspace,
  runRoleCheck,
  keys,
}: ConnectAccountPanelProps): React.ReactElement {
  const { run_role_setup: setup } = workspace;
  const savedArn = workspace.run_role_arn ?? null;
  const pendingArn = workspace.pending_run_role_arn ?? null;
  const [changing, setChanging] = useState(savedArn === null);
  const pending =
    pendingArn === null ? null : (
      <PendingRole
        workspace={workspace}
        arn={pendingArn}
        check={
          runRoleCheck?.pending?.role_arn === pendingArn
            ? runRoleCheck.pending
            : null
        }
        keys={keys}
      />
    );
  if (savedArn !== null && !changing) {
    return (
      <div className="space-y-5">
        {pending}
        <ConnectedRole
          workspace={workspace}
          arn={savedArn}
          runRoleCheck={runRoleCheck}
          keys={keys}
          onChange={() => {
            setChanging(true);
          }}
        />
      </div>
    );
  }
  return (
    <div className="space-y-5">
      {pending}
      {savedArn === null ? null : (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-raised px-3 py-2 text-sm">
          <span className="min-w-0 text-text-muted">
            Current role:{' '}
            <code className="font-mono text-xs break-all text-text">
              {savedArn}
            </code>
          </span>
          <Button
            variant="secondary"
            onClick={() => {
              setChanging(false);
            }}
          >
            Keep the current role
          </Button>
        </div>
      )}
      <p className="max-w-prose text-sm text-text-muted">
        Runs assume an IAM role in your AWS account to read and write your
        infrastructure. Quick setup creates that role with one AWS
        CloudFormation stack. The role trusts only the runner, and only when it
        presents this workspace's id.
      </p>
      <QuickSetup workspace={workspace} keys={keys} />
      <ConnectionCheck
        workspace={workspace}
        runRoleCheck={runRoleCheck}
        keys={keys}
      />
      <details className="group rounded-md border border-line">
        <summary className="cursor-pointer px-3 py-2 text-sm text-text select-none hover:text-text-strong">
          Set up the role manually
        </summary>
        <div className="space-y-5 border-t border-line p-3">
          <p className="max-w-prose text-sm text-text-muted">
            Create the role with your own Terraform, CloudFormation or the AWS
            CLI, then save its ARN.
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
          <RoleSnippets workspace={workspace} />
          <RoleArnForm workspace={workspace} keys={keys} />
        </div>
      </details>
    </div>
  );
}

/** The connected state: the account, the role ARN, its live status, and Change role. */
function ConnectedRole({
  workspace,
  arn,
  runRoleCheck,
  keys,
  onChange,
}: {
  workspace: Workspace;
  arn: string;
  runRoleCheck: RunRoleCheck | null;
  keys: WorkspaceKeys;
  onChange: () => void;
}): React.ReactElement {
  const accountId = accountIdFromArn(arn);
  return (
    <section
      aria-label="Connected AWS account"
      data-testid="connected-account"
      className="rounded-lg border border-line bg-panel"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-text-strong">
            AWS account{' '}
            <span className="font-mono">{accountId ?? 'unknown'}</span>
          </h3>
          <p className="mt-0.5 text-xs text-text-muted">
            Runs assume this role to read and write your infrastructure.
          </p>
        </div>
        <Button variant="secondary" onClick={onChange}>
          Change role
        </Button>
      </div>
      <div className="space-y-4 px-4 py-4">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
          <ValueRow label="Role ARN" value={arn} />
        </dl>
        <ConnectionCheck
          workspace={workspace}
          runRoleCheck={runRoleCheck}
          keys={keys}
        />
      </div>
    </section>
  );
}

/**
 * A role staged beside the working one, as HCP Terraform keeps an integration
 * in place until the new credentials are proven.
 *
 * Runs keep the current role. The verification run is plan only and assumes the
 * staged role, and the workspace frame switches over once it connects.
 * Discarding the staged role leaves the current one as it is.
 */
function PendingRole({
  workspace,
  arn,
  check,
  keys,
}: {
  workspace: Workspace;
  arn: string;
  check: PendingRunRoleCheck | null;
  keys: WorkspaceKeys;
}): React.ReactElement {
  const navigate = useNavigate();
  const context = useOptionalWorkspace();
  const latest = latestUploaded(context?.versions ?? []);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const discard = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        pending_run_role_arn: null,
      }),
    keys.workspace
  );
  const accountId = accountIdFromArn(arn);
  const status = check?.status ?? 'unverified';

  const verify = async (): Promise<void> => {
    if (latest === null) {
      return;
    }
    setVerifying(true);
    setError(null);
    try {
      const run = await api.createRun({
        workspace_id: workspace.workspace_id,
        config_version_id: latest.config_version_id,
        run_role_check: true,
        message: `Verify the run role in AWS account ${accountId ?? 'unknown'}`,
      });
      invalidateQueries(keys.runs);
      void navigate(
        runPath({ run_id: run.run_id, workspace_id: workspace.workspace_id })
      );
    } catch (thrown) {
      setError(thrown);
      setVerifying(false);
    }
  };

  return (
    <section
      aria-label="Pending AWS account"
      data-testid="pending-account"
      className="rounded-lg border border-line bg-panel"
    >
      <div className="border-b border-line px-4 py-3">
        <h3 className="text-sm font-semibold text-text-strong">
          Switching to AWS account{' '}
          <span className="font-mono">{accountId ?? 'unknown'}</span>
        </h3>
        <p className="mt-0.5 text-xs text-text-muted">
          Runs keep the current role until a verification run assumes this one.
          The workspace switches over as soon as it connects.
        </p>
      </div>
      <div className="space-y-4 px-4 py-4">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
          <ValueRow label="Role ARN" value={arn} />
        </dl>
        {status === 'failed' ? (
          <StatusLine tone="bad" testValue="failed">
            The verification run could not assume this role.{' '}
            {check?.error ?? ''}
          </StatusLine>
        ) : status === 'connected' ? (
          <StatusLine tone="ok" testValue="connected">
            The verification run assumed this role. Switching the workspace
            over.
          </StatusLine>
        ) : (
          <StatusLine tone="neutral" testValue="unverified">
            Not verified yet. Once the AWS CloudFormation stack is created,
            start the verification run. It only plans, and changes nothing.
          </StatusLine>
        )}
        {latest === null ? (
          <p className="text-xs text-text-faint">
            Upload a configuration version first, so the verification run has
            something to plan.
          </p>
        ) : null}
        <ErrorNotice error={error ?? discard.error} />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            busy={verifying}
            busyLabel="Starting the verification run"
            disabled={latest === null || status === 'connected'}
            onClick={() => {
              void verify();
            }}
          >
            Start verification run
          </Button>
          <Button
            variant="secondary"
            busy={discard.isMutating}
            busyLabel="Discarding the new role"
            onClick={() => {
              void discard
                .mutate()
                .then(() => {
                  invalidateQueries(keys.runRoleCheck);
                })
                .catch(() => undefined);
            }}
          >
            Discard the new role
          </Button>
        </div>
      </div>
    </section>
  );
}

/**
 * The account id and policy form that opens AWS CloudFormation quick create.
 *
 * The tab is opened blank inside the click, before the request, because a
 * window opened after an await is treated as a popup and blocked. It is pointed
 * at the console once the link arrives, and closed again if the request fails.
 */
function QuickSetup({
  workspace,
  keys,
}: {
  workspace: Workspace;
  keys: WorkspaceKeys;
}): React.ReactElement {
  const savedAccount = accountIdFromArn(workspace.run_role_arn);
  const [accountId, setAccountId] = useState(savedAccount ?? '');
  const [permissions, setPermissions] =
    useState<RunRolePermissions>('administrator');
  const [problem, setProblem] = useState<string | null>(null);
  const [launched, setLaunched] = useState<RunRoleQuickSetup | null>(null);
  const [blocked, setBlocked] = useState(false);

  const start = useMutationWithRefetch(
    (body: { account_id: string; permissions: RunRolePermissions }) =>
      api.startRunRoleQuickSetup(workspace.workspace_id, body),
    keys.workspace
  );
  const choice =
    PERMISSIONS_CHOICES.find((entry) => entry.id === permissions) ??
    PERMISSIONS_CHOICES[0];

  const launch = async (): Promise<void> => {
    const why = accountIdProblem(accountId);
    if (why !== null) {
      setProblem(why);
      return;
    }
    setProblem(null);
    const tab = window.open('', '_blank');
    if (tab !== null) {
      tab.opener = null;
    }
    try {
      const answer = await start.mutate({
        account_id: normalizeAccountId(accountId),
        permissions,
      });
      invalidateQueries(keys.runRoleCheck);
      setLaunched(answer);
      setBlocked(tab === null);
      if (tab !== null) {
        tab.location.href = answer.console_url;
      }
    } catch {
      tab?.close();
    }
  };

  return (
    <section
      aria-label="AWS quick setup"
      className="rounded-lg border border-line bg-panel"
    >
      <div className="border-b border-line px-4 py-3">
        <h3 className="text-sm font-semibold text-text-strong">
          AWS quick setup
        </h3>
        <p className="mt-0.5 text-xs text-text-muted">
          Opens AWS CloudFormation with the role filled in. Nothing to copy
          back: the role ARN is saved here as you open it. A workspace that
          already has a role keeps it until the new one is verified.
        </p>
      </div>
      <form
        aria-label="AWS quick setup"
        className="space-y-3 px-4 py-4"
        onSubmit={(event) => {
          event.preventDefault();
          void launch();
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label="AWS account ID"
            error={problem}
            hint="The account the role is created in and runs act on."
          >
            {(control) => (
              <input
                {...control}
                value={accountId}
                inputMode="numeric"
                placeholder="123456789012"
                spellCheck={false}
                autoComplete="off"
                onChange={(event) => {
                  setAccountId(event.target.value);
                  setProblem(null);
                }}
                className={`${INPUT_CLASS} font-mono`}
              />
            )}
          </Field>
          <Field label="Permissions policy" hint={choice?.hint}>
            {(control) => (
              <select
                {...control}
                value={permissions}
                onChange={(event) => {
                  setPermissions(event.target.value as RunRolePermissions);
                }}
                className={INPUT_CLASS}
              >
                {PERMISSIONS_CHOICES.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <ErrorNotice error={start.error} />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="submit"
            variant={launched === null ? 'primary' : 'secondary'}
            busy={start.isMutating}
            busyLabel="Preparing the AWS CloudFormation stack"
            disabled={accountId.trim() === ''}
          >
            {launched === null
              ? 'Open AWS CloudFormation'
              : 'Open AWS CloudFormation again'}
          </Button>
        </div>
      </form>
      {launched === null ? null : (
        <LaunchedSteps launched={launched} blocked={blocked} />
      )}
    </section>
  );
}

/** What is left to do in AWS once the quick create page is open. */
function LaunchedSteps({
  launched,
  blocked,
}: {
  launched: RunRoleQuickSetup;
  blocked: boolean;
}): React.ReactElement {
  return (
    <div
      data-testid="quick-setup-steps"
      className="space-y-3 border-t border-line px-4 py-4 text-sm"
    >
      <p role="status" className="text-text">
        {launched.pending ? 'New role staged: ' : 'Role ARN saved: '}
        <code className="font-mono text-xs break-all text-text-strong">
          {launched.role_arn}
        </code>
      </p>
      {launched.pending ? (
        <p className="text-text-muted">
          Runs keep the current role until a verification run assumes this one,
          so the workspace keeps working if the stack is never created.
        </p>
      ) : null}
      {blocked ? (
        <p className="text-text-muted">
          Your browser blocked the new tab.{' '}
          <a
            href={launched.console_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent underline-offset-2 hover:underline"
          >
            Open AWS CloudFormation
          </a>
          .
        </p>
      ) : null}
      <ol className="list-decimal space-y-1.5 pl-5 text-text-muted marker:text-text-faint">
        <li>
          In AWS, sign in to account{' '}
          <code className="font-mono text-text">{launched.account_id}</code> if
          you are not already.
        </li>
        <li>
          Tick the acknowledgement that AWS CloudFormation might create IAM
          resources with custom names, then choose Create stack.
        </li>
        {launched.pending ? (
          <li>
            Start the verification run above once the stack reaches
            CREATE_COMPLETE. The workspace switches to the new role when it
            connects.
          </li>
        ) : (
          <li>
            Start a plan only run once the stack reaches CREATE_COMPLETE. The
            run proves the runner can assume the role, and the connection below
            turns connected.
          </li>
        )}
      </ol>
      <p className="text-xs text-text-faint">
        The link works for one hour. Open it again here for a fresh one.
      </p>
    </div>
  );
}

/** The live connection state and the button that records it on the workspace. */
function ConnectionCheck({
  workspace,
  runRoleCheck,
  keys,
}: {
  workspace: Workspace;
  runRoleCheck: RunRoleCheck | null;
  keys: WorkspaceKeys;
}): React.ReactElement {
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [result, setResult] = useState<RunRoleCheck | null>(null);
  const unsaved = (workspace.run_role_arn ?? null) === null;

  const shownCheck = useRef(runRoleCheck);
  useEffect(() => {
    if (shownCheck.current !== runRoleCheck) {
      shownCheck.current = runRoleCheck;
      setResult(null);
    }
  }, [runRoleCheck]);

  const shownArn = useRef(workspace.run_role_arn);
  useEffect(() => {
    if (shownArn.current !== workspace.run_role_arn) {
      shownArn.current = workspace.run_role_arn;
      setResult(null);
    }
  }, [workspace.run_role_arn]);

  const check = async (): Promise<void> => {
    setChecking(true);
    setCheckError(null);
    try {
      const outcome = await api.checkRunRole(workspace.workspace_id);
      setResult(outcome);
      invalidateQueries(keys.workspace);
      invalidateQueries(keys.runRoleCheck);
    } catch (thrown) {
      setCheckError(thrown);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant="secondary"
          busy={checking}
          busyLabel="Checking the latest runs"
          disabled={unsaved}
          title={unsaved ? 'Save a role ARN first.' : undefined}
          onClick={() => {
            void check();
          }}
        >
          Check connection
        </Button>
        <ConnectionStatus
          workspace={workspace}
          check={result ?? runRoleCheck}
        />
      </div>
      <ErrorNotice error={checkError} />
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
        <code className="min-w-0 truncate rounded border border-line bg-raised px-1.5 py-1 font-mono text-xs text-text">
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

/** The ARN input for a role created outside quick setup. */
function RoleArnForm({
  workspace,
  keys,
}: {
  workspace: Workspace;
  keys: WorkspaceKeys;
}): React.ReactElement {
  const { run_role_setup: setup } = workspace;
  const [arn, setArn] = useState(workspace.run_role_arn ?? '');
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setArn(workspace.run_role_arn ?? '');
    setProblem(null);
  }, [workspace.run_role_arn]);

  const save = useMutationWithRefetch(
    (value: string) =>
      api.updateWorkspace(workspace.workspace_id, { run_role_arn: value }),
    keys.workspace
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
      invalidateQueries(keys.runRoleCheck);
      setSaved(true);
    } catch {
      return;
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
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="submit"
          variant={dirty ? 'primary' : 'secondary'}
          busy={save.isMutating}
          busyLabel="Saving the role ARN"
          disabled={arn.trim() === '' || !dirty}
        >
          Save
        </Button>
        {saved ? (
          <span role="status" className="text-sm text-success">
            Saved. Start a plan only run to prove the runner can assume it.
          </span>
        ) : null}
      </div>
    </form>
  );
}

/** What the runner's record says about the role, fresh or as last recorded. */
function ConnectionStatus({
  workspace,
  check,
}: {
  workspace: Workspace;
  check: RunRoleCheck | null;
}): React.ReactElement | null {
  const status = accountStatus(workspace, check);
  const checkedAt = status.checkedAt;
  if (status.state === 'missing') {
    return null;
  }
  if (status.state === 'connected') {
    return (
      <StatusLine tone="ok" testValue="connected">
        The runner assumed this role in account{' '}
        <code className="font-mono">{status.accountId ?? 'unknown'}</code>
        {checkedAt === null ? '.' : `, ${formatDateTime(checkedAt)}.`}
      </StatusLine>
    );
  }
  if (status.state === 'failed') {
    return (
      <StatusLine tone="bad" testValue="failed">
        The runner could not assume the role
        {checkedAt === null ? '' : ` on ${formatDateTime(checkedAt)}`}.{' '}
        {status.error ?? ''}
      </StatusLine>
    );
  }
  return (
    <StatusLine tone="neutral" testValue="unverified">
      {status.error ??
        'Not verified yet. The first run proves the runner can assume the role.'}
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
    ok: 'bg-success',
    bad: 'bg-danger',
    neutral: 'bg-surface-400',
  }[tone];
  const text = {
    ok: 'text-success',
    bad: 'text-danger',
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
