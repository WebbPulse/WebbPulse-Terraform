/** The notifications page: where a workspace sends word of its runs, and a test send for each. */

import { Fragment, useState } from 'react';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type NotificationConfiguration,
  type NotificationConfigurationList,
  type NotificationDelivery,
  type NotificationTrigger,
} from '../../../api';
import {
  Button,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  RelativeTime,
  SegmentedControl,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import {
  DESTINATIONS,
  MAX_NOTIFICATIONS,
  TRIGGERS,
  createBody,
  deliverySummary,
  destinationLabel,
  draftFrom,
  draftProblem,
  testSendError,
  triggerLabel,
  updateBody,
  type NotificationDraft,
} from './notifications';

/** The refetch key for one workspace's notification configurations. */
function notificationsKey(workspaceId: string): string {
  return `notifications:${workspaceId}`;
}

/** The notifications page. */
export function NotificationsSettings(): React.ReactElement {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const auth = useQueryAuth();
  const queryKey = notificationsKey(workspaceId);
  const query = usePolledQuery<NotificationConfigurationList>(
    ({ signal }) => api.listNotificationConfigurations(workspaceId, { signal }),
    { intervalMs: 60_000, queryKey, auth }
  );
  const [form, setForm] = useState<{
    editing: NotificationConfiguration | null;
  } | null>(null);
  const items = query.data?.items ?? [];
  const full = items.length >= MAX_NOTIFICATIONS;

  const openCreate = (): void => {
    setForm({ editing: null });
  };

  return (
    <div className="max-w-4xl space-y-5">
      <div>
        <h2 className="text-sm font-semibold text-text-strong">
          Notifications
        </h2>
        <p className="mt-1 max-w-prose text-sm text-text-muted">
          Send a message to Slack, Discord or any webhook when a run in this
          workspace reaches a stage you choose. Webhook URLs and tokens are
          write only and never shown again.
        </p>
      </div>
      <ErrorNotice error={query.error} />
      <section aria-labelledby="notification-list" className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h3
            id="notification-list"
            className="text-sm font-medium text-text-strong"
          >
            Notification configurations{' '}
            <span className="text-text-faint">
              ({items.length} of {MAX_NOTIFICATIONS})
            </span>
          </h3>
          {form === null && items.length > 0 ? (
            <Button
              size="sm"
              disabled={full}
              title={
                full
                  ? `A workspace holds at most ${String(MAX_NOTIFICATIONS)} notifications.`
                  : undefined
              }
              onClick={openCreate}
            >
              + Create notification
            </Button>
          ) : null}
        </div>
        {form === null ? null : (
          <NotificationForm
            key={form.editing?.id ?? 'new'}
            workspaceId={workspaceId}
            queryKey={queryKey}
            editing={form.editing}
            onDone={() => {
              setForm(null);
            }}
          />
        )}
        {query.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading notifications" className="size-4" />
            Loading notifications
          </div>
        ) : items.length === 0 ? (
          form === null && query.error === null ? (
            <EmptyState
              title="No notifications yet."
              hint="Create one to hear about runs in Slack, Discord or your own webhook."
              action={
                <Button variant="primary" onClick={openCreate}>
                  + Create notification
                </Button>
              }
            />
          ) : null
        ) : (
          <NotificationTable
            workspaceId={workspaceId}
            queryKey={queryKey}
            items={items}
            onEdit={(item) => {
              setForm({ editing: item });
            }}
          />
        )}
      </section>
    </div>
  );
}

/** A test send's outcome, or why it did not go out, keyed to its configuration. */
type TestResult =
  | { id: string; delivery: NotificationDelivery }
  | { id: string; error: unknown };

/** The table of configurations, each row with its toggle, a test send, an edit and a delete. */
function NotificationTable({
  workspaceId,
  queryKey,
  items,
  onEdit,
}: {
  workspaceId: string;
  queryKey: string;
  items: NotificationConfiguration[];
  onEdit: (item: NotificationConfiguration) => void;
}): React.ReactElement {
  const [testing, setTesting] = useState<string | null>(null);
  const [result, setResult] = useState<TestResult | null>(null);
  const [deleting, setDeleting] = useState<NotificationConfiguration | null>(
    null
  );
  const toggle = useMutationWithRefetch(
    (item: NotificationConfiguration) =>
      api.updateNotificationConfiguration(workspaceId, item.id, {
        enabled: !item.enabled,
      }),
    queryKey
  );
  const verify = useMutationWithRefetch(
    (id: string) => api.verifyNotificationConfiguration(workspaceId, id),
    queryKey
  );

  const sendTest = async (item: NotificationConfiguration): Promise<void> => {
    setTesting(item.id);
    setResult(null);
    try {
      const delivery = await verify.mutate(item.id);
      setResult({ id: item.id, delivery });
    } catch (thrown) {
      setResult({ id: item.id, error: thrown });
    } finally {
      setTesting(null);
    }
  };

  return (
    <div className="space-y-2">
      <ErrorNotice error={toggle.error} />
      <Table label="Notifications">
        <thead>
          <tr>
            <Th>Name</Th>
            <Th>Triggers</Th>
            <Th>Last delivery</Th>
            <Th>Status</Th>
            <Th className="text-right">Actions</Th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <Fragment key={item.id}>
              <Tr>
                <Td className="max-w-xs">
                  <span className="block truncate font-medium text-text-strong">
                    {item.name}
                  </span>
                  <span className="mt-0.5 flex items-center gap-1.5 text-xs text-text-faint">
                    <span className="rounded border border-line-strong px-1 text-[10px] text-text-muted">
                      {destinationLabel(item.destination_type)}
                    </span>
                    <span className="truncate font-mono">
                      {item.url_masked}
                    </span>
                  </span>
                </Td>
                <Td className="text-xs text-text-muted">
                  <TriggerList triggers={item.triggers} />
                </Td>
                <Td className="text-xs whitespace-nowrap">
                  <LastDelivery delivery={item.last_delivery ?? null} />
                </Td>
                <Td>
                  <EnabledSwitch
                    item={item}
                    busy={toggle.isMutating}
                    onToggle={() => {
                      void toggle.mutate(item).catch(() => undefined);
                    }}
                  />
                </Td>
                <Td className="text-right whitespace-nowrap">
                  <Button
                    size="sm"
                    variant="ghost"
                    busy={testing === item.id}
                    busyLabel="Sending a test"
                    disabled={testing !== null && testing !== item.id}
                    onClick={() => {
                      void sendTest(item);
                    }}
                  >
                    Send test
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      onEdit(item);
                    }}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-danger hover:underline"
                    onClick={() => {
                      setDeleting(item);
                    }}
                  >
                    Delete
                  </Button>
                </Td>
              </Tr>
              {result !== null && result.id === item.id ? (
                <tr className="border-t border-line">
                  <td colSpan={5} className="px-3 py-2">
                    <TestResultNotice
                      result={result}
                      onDismiss={() => {
                        setResult(null);
                      }}
                    />
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
        </tbody>
      </Table>
      <Dialog
        open={deleting !== null}
        onClose={() => {
          setDeleting(null);
        }}
        title="Delete notification"
        description="This cannot be undone."
      >
        {deleting === null ? null : (
          <DeleteConfirm
            workspaceId={workspaceId}
            queryKey={queryKey}
            item={deleting}
            onDone={() => {
              if (result?.id === deleting.id) {
                setResult(null);
              }
              setDeleting(null);
            }}
          />
        )}
      </Dialog>
    </div>
  );
}

/** The triggers a configuration fires on, or a note that only test sends go out. */
function TriggerList({
  triggers,
}: {
  triggers: readonly NotificationTrigger[];
}): React.ReactElement {
  if (triggers.length === 0) {
    return <span className="text-text-faint">Test sends only</span>;
  }
  if (triggers.length === TRIGGERS.length) {
    return <span>All events</span>;
  }
  return (
    <ul className="flex flex-wrap gap-1" aria-label="Triggers">
      {triggers.map((trigger) => (
        <li
          key={trigger}
          className="rounded-full border border-line-strong bg-raised px-1.5 py-0.5 text-[11px] text-text"
        >
          {triggerLabel(trigger)}
        </li>
      ))}
    </ul>
  );
}

/** The dot colour for each delivery status. */
const DELIVERY_DOT: Record<NotificationDelivery['status'], string> = {
  succeeded: 'bg-success',
  failed: 'bg-danger',
  retrying: 'bg-warning',
};

/** The newest delivery's outcome and when it was attempted, or a dash before the first. */
function LastDelivery({
  delivery,
}: {
  delivery: NotificationDelivery | null;
}): React.ReactElement {
  if (delivery === null) {
    return <span className="text-text-faint">Never sent</span>;
  }
  return (
    <span
      className="inline-flex items-center gap-1.5"
      data-testid="last-delivery"
      data-status={delivery.status}
      title={delivery.error ?? undefined}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 rounded-full ${DELIVERY_DOT[delivery.status]}`}
      />
      <span className="text-text">{deliverySummary(delivery)}</span>
      <RelativeTime iso={delivery.attempted_at} className="text-text-faint" />
    </span>
  );
}

/** A switch that turns a configuration on or off. */
function EnabledSwitch({
  item,
  busy,
  onToggle,
}: {
  item: NotificationConfiguration;
  busy: boolean;
  onToggle: () => void;
}): React.ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={item.enabled}
      aria-label={`Enable ${item.name}`}
      disabled={busy}
      onClick={onToggle}
      className="inline-flex items-center gap-2 rounded-md text-xs text-text-muted focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none disabled:opacity-50"
    >
      <span
        aria-hidden="true"
        className={`relative inline-flex h-4 w-7 shrink-0 rounded-full transition-colors ${
          item.enabled ? 'bg-accent' : 'bg-line-strong'
        }`}
      >
        <span
          className={`absolute top-0.5 size-3 rounded-full bg-panel shadow-xs transition-transform ${
            item.enabled ? 'translate-x-3.5' : 'translate-x-0.5'
          }`}
        />
      </span>
      {item.enabled ? 'Enabled' : 'Disabled'}
    </button>
  );
}

/** What a test send came back with: the receiver's answer, or why it was not sent. */
function TestResultNotice({
  result,
  onDismiss,
}: {
  result: TestResult;
  onDismiss: () => void;
}): React.ReactElement {
  const failed = 'error' in result || result.delivery.status !== 'succeeded';
  return (
    <div
      role="status"
      data-testid="test-result"
      className={`flex items-start gap-3 rounded-md border px-3 py-2 text-sm ${
        failed
          ? 'border-danger-line bg-danger-soft'
          : 'border-success-line bg-success-soft'
      }`}
    >
      <div className="min-w-0 flex-1 space-y-1">
        {'error' in result ? (
          <p className="text-danger">{testSendError(result.error)}</p>
        ) : (
          <>
            <p className={failed ? 'text-danger' : 'text-success'}>
              Test send: {deliverySummary(result.delivery)}
            </p>
            {(result.delivery.error ?? '') === '' ? null : (
              <p className="text-text-muted">{result.delivery.error}</p>
            )}
            {(result.delivery.response_excerpt ?? '') === '' ? null : (
              <pre className="max-h-24 overflow-auto rounded bg-raised px-2 py-1 font-mono text-xs whitespace-pre-wrap text-text-muted">
                {result.delivery.response_excerpt}
              </pre>
            )}
          </>
        )}
      </div>
      <Button size="sm" variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

/** The confirmation inside the delete dialog. */
function DeleteConfirm({
  workspaceId,
  queryKey,
  item,
  onDone,
}: {
  workspaceId: string;
  queryKey: string;
  item: NotificationConfiguration;
  onDone: () => void;
}): React.ReactElement {
  const { mutate, isMutating, error } = useMutationWithRefetch(
    () => api.deleteNotificationConfiguration(workspaceId, item.id),
    queryKey
  );
  return (
    <div className="space-y-4">
      <p className="text-sm text-text-muted">
        {item.name} stops receiving run notifications, and its webhook URL
        {item.has_token ? ' and token are' : ' is'} discarded.
      </p>
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          variant="danger"
          busy={isMutating}
          busyLabel="Deleting the notification"
          onClick={() => {
            void mutate()
              .then(onDone)
              .catch(() => undefined);
          }}
        >
          Delete notification
        </Button>
      </div>
    </div>
  );
}

/**
 * The form that creates or edits one configuration.
 *
 * The URL and token are write only, so editing starts with both blank: a blank
 * URL or token keeps the stored one rather than round tripping a value the
 * browser was never given.
 */
function NotificationForm({
  workspaceId,
  queryKey,
  editing,
  onDone,
}: {
  workspaceId: string;
  queryKey: string;
  editing: NotificationConfiguration | null;
  onDone: () => void;
}): React.ReactElement {
  const [draft, setDraft] = useState<NotificationDraft>(() =>
    draftFrom(editing)
  );
  const [attempted, setAttempted] = useState(false);
  const problem = draftProblem(draft, editing);
  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      editing === null
        ? api.createNotificationConfiguration(workspaceId, createBody(draft))
        : api.updateNotificationConfiguration(
            workspaceId,
            editing.id,
            updateBody(editing, draft)
          ),
    queryKey
  );

  const patch = (changes: Partial<NotificationDraft>): void => {
    setDraft((current) => ({ ...current, ...changes }));
  };

  const submit = async (): Promise<void> => {
    setAttempted(true);
    if (problem !== null) {
      return;
    }
    try {
      await mutate();
      onDone();
    } catch {
      return;
    }
  };

  const destinationChanged =
    editing !== null && editing.destination_type !== draft.destination;
  const generic = draft.destination === 'generic';
  const storedToken =
    editing !== null &&
    editing.destination_type === 'generic' &&
    editing.has_token;

  return (
    <form
      aria-label={
        editing === null ? 'Create a notification' : 'Edit the notification'
      }
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h4 className="text-sm font-semibold text-text-strong">
        {editing === null ? 'Create a notification' : `Edit ${editing.name}`}
      </h4>
      <Field label="Name">
        {(control) => (
          <input
            {...control}
            autoFocus
            autoComplete="off"
            value={draft.name}
            onChange={(event) => {
              patch({ name: event.target.value });
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <div className="space-y-1 text-sm">
        <p className="text-text-muted">Destination</p>
        <SegmentedControl
          label="Destination"
          segments={DESTINATIONS}
          value={draft.destination}
          onChange={(destination) => {
            patch({ destination });
          }}
        />
      </div>
      <Field
        label="Webhook URL"
        hint={
          editing !== null && !destinationChanged
            ? `Leave blank to keep ${editing.url_masked}.`
            : destinationChanged
              ? 'Changing the destination needs a new URL.'
              : undefined
        }
      >
        {(control) => (
          <input
            {...control}
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder={
              draft.destination === 'slack'
                ? 'https://hooks.slack.com/services/...'
                : draft.destination === 'discord'
                  ? 'https://discord.com/api/webhooks/...'
                  : 'https://'
            }
            value={draft.url}
            onChange={(event) => {
              patch({ url: event.target.value });
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      {generic ? (
        <div className="space-y-2">
          <Field
            label="Token"
            hint={
              storedToken && !draft.clearToken
                ? 'A token is set. Leave blank to keep it.'
                : 'Optional. Signs each payload with HMAC SHA-512 in the X-TFE-Notification-Signature header.'
            }
          >
            {(control) => (
              <input
                {...control}
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                disabled={draft.clearToken}
                value={draft.token}
                onChange={(event) => {
                  patch({ token: event.target.value });
                }}
                className={`${INPUT_CLASS} font-mono`}
              />
            )}
          </Field>
          {storedToken ? (
            <label className="flex items-center gap-2 text-sm text-text-muted">
              <input
                type="checkbox"
                checked={draft.clearToken}
                onChange={(event) => {
                  patch({ clearToken: event.target.checked, token: '' });
                }}
                className="size-3.5 accent-accent"
              />
              Remove the token
            </label>
          ) : null}
        </div>
      ) : null}
      <fieldset className="space-y-2">
        <legend className="text-sm text-text-muted">Triggers</legend>
        <p className="text-xs text-text-faint">
          With none checked, only test sends are delivered.
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {TRIGGERS.map((trigger) => (
            <label
              key={trigger.id}
              className="flex items-start gap-2 rounded-md border border-line px-2.5 py-2 text-sm"
            >
              <input
                type="checkbox"
                checked={draft.triggers.includes(trigger.id)}
                onChange={(event) => {
                  patch({
                    triggers: event.target.checked
                      ? [...draft.triggers, trigger.id]
                      : draft.triggers.filter((id) => id !== trigger.id),
                  });
                }}
                className="mt-0.5 size-3.5 accent-accent"
              />
              <span>
                <span className="block text-text-strong">{trigger.label}</span>
                <span className="block text-xs text-text-faint">
                  {trigger.hint}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <label className="flex items-center gap-2 text-sm text-text-muted">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => {
            patch({ enabled: event.target.checked });
          }}
          className="size-3.5 accent-accent"
        />
        Enabled
      </label>
      {attempted && problem !== null ? (
        <p role="alert" className="text-sm text-danger">
          {problem}
        </p>
      ) : null}
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving the notification"
        >
          {editing === null ? 'Create notification' : 'Save notification'}
        </Button>
      </div>
    </form>
  );
}
