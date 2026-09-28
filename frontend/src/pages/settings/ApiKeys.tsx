/** Account settings for API keys: list the caller's keys, mint one and revoke one. */

import { useState } from 'react';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useAuth, useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type ApiKey,
  type ApiKeyCreated,
  type ApiKeyList,
} from '../../api';
import {
  Button,
  CopyButton,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Table,
  Td,
  Th,
  Tr,
  formatDateTime,
} from '../../components';
import { RowsSkeleton } from './Skeleton';

/** The refetch key for the key list. */
export const API_KEYS_KEY = 'api-keys';

/** The page's crumbs, the page itself left to the title. */
const CRUMBS = [{ label: 'Account' }] as const;

/** The expiry a new key starts with, in days. The API's own default. */
export const DEFAULT_EXPIRY_DAYS = 90;

/** The expiries offered, in days. The API refuses anything past 365. */
export const EXPIRY_CHOICES: readonly number[] = [7, 30, 60, 90, 180, 365];

/** The longest name the API accepts. */
const NAME_MAX = 120;

/** The `scope` claim of a JWT, split, or an empty list when it cannot be read. */
export function scopesFromToken(token: string | null): string[] {
  const payload = token?.split('.')[1];
  if (payload === undefined || payload === '') {
    return [];
  }
  try {
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/');
    const claims = JSON.parse(atob(padded)) as { scope?: unknown };
    return typeof claims.scope === 'string'
      ? claims.scope.split(' ').filter((scope) => scope !== '')
      : [];
  } catch {
    return [];
  }
}

/** Where a key stands now, from its revocation and expiry. */
export function keyStatus(
  key: ApiKey,
  now: number = Date.now()
): 'active' | 'revoked' | 'expired' {
  if (key.revoked_at !== null && key.revoked_at !== undefined) {
    return 'revoked';
  }
  if (
    key.expires_at !== null &&
    key.expires_at !== undefined &&
    key.expires_at * 1000 <= now
  ) {
    return 'expired';
  }
  return 'active';
}

/** The ISO expiry `days` from `now`. */
export function expiryFrom(days: number, now: number = Date.now()): string {
  return new Date(now + days * 86_400_000).toISOString();
}

/** The API keys page. */
export function ApiKeys(): React.ReactElement {
  const auth = useQueryAuth();
  const keys = usePolledQuery<ApiKeyList>(
    ({ signal }) => api.listApiKeys({ signal }),
    { intervalMs: 60_000, queryKey: API_KEYS_KEY, auth }
  );
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const items = keys.data?.items ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="API keys"
        crumbs={CRUMBS}
        description="Keys let the provider, the CLI and agents act as you, within the scopes you give them. A key cannot mint another key."
        actions={
          <Button
            variant="primary"
            onClick={() => {
              setCreating(true);
            }}
          >
            Create an API key
          </Button>
        }
      />
      <ErrorNotice error={keys.error} />
      {created === null ? null : (
        <NewKeyNotice
          created={created}
          onDismiss={() => {
            setCreated(null);
          }}
        />
      )}
      {keys.isLoading ? (
        <RowsSkeleton label="Loading API keys" />
      ) : keys.data === null ? null : items.length === 0 ? (
        <EmptyState
          title="No API keys yet"
          hint="Create one for the provider or an agent. Its secret is shown once."
        />
      ) : (
        <KeyTable
          keys={items}
          onRevoke={(key) => {
            setRevoking(key);
          }}
        />
      )}
      {creating ? (
        <CreateKeyDialog
          onClose={() => {
            setCreating(false);
          }}
          onCreated={(key) => {
            setCreating(false);
            setCreated(key);
          }}
        />
      ) : null}
      {revoking === null ? null : (
        <RevokeKeyDialog
          apiKey={revoking}
          onClose={() => {
            setRevoking(null);
          }}
        />
      )}
    </div>
  );
}

/** The one sight of a new key's secret. */
function NewKeyNotice({
  created,
  onDismiss,
}: {
  created: ApiKeyCreated;
  onDismiss: () => void;
}): React.ReactElement {
  return (
    <section
      aria-label="New API key"
      className="space-y-3 rounded-lg border border-accent-line bg-accent-soft p-4"
    >
      <div>
        <h2 className="text-sm font-medium text-text-strong">
          {`Key "${created.name}" created`}
        </h2>
        <p className="mt-0.5 text-sm text-text-muted">
          Copy it now. It is not stored and will not be shown again.
        </p>
      </div>
      <div className="flex items-center gap-2">
        <code
          data-testid="new-api-key"
          className="min-w-0 flex-1 truncate rounded-md border border-line bg-panel px-2.5 py-1.5 font-mono text-xs text-text-strong"
        >
          {created.key}
        </code>
        <CopyButton value={created.key} subject="API key" variant="secondary" />
      </div>
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          Done
        </Button>
      </div>
    </section>
  );
}

/** The caller's keys. */
function KeyTable({
  keys,
  onRevoke,
}: {
  keys: ApiKey[];
  onRevoke: (key: ApiKey) => void;
}): React.ReactElement {
  return (
    <Table label="API keys">
      <thead>
        <tr>
          <Th>Name</Th>
          <Th>Key</Th>
          <Th>Scopes</Th>
          <Th>Created</Th>
          <Th>Last used</Th>
          <Th>Expires</Th>
          <Th>
            <span className="sr-only">Actions</span>
          </Th>
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => {
          const status = keyStatus(key);
          return (
            <Tr key={key.key_id}>
              <Td className="font-medium text-text-strong">
                {key.name}
                {status === 'active' ? null : (
                  <span className="ml-2 rounded-sm bg-raised px-1.5 py-0.5 text-xs font-normal text-text-muted">
                    {status === 'revoked' ? 'Revoked' : 'Expired'}
                  </span>
                )}
              </Td>
              <Td className="font-mono text-xs text-text-muted">
                {`${key.prefix}...`}
              </Td>
              <Td className="text-xs text-text-muted">
                {key.scopes.length === 0 ? 'None' : key.scopes.join(', ')}
              </Td>
              <Td className="whitespace-nowrap text-text-muted">
                <RelativeTime iso={key.created_at} />
              </Td>
              <Td className="whitespace-nowrap text-text-muted">
                {key.last_used_at === null || key.last_used_at === undefined ? (
                  'Never'
                ) : (
                  <RelativeTime iso={key.last_used_at} />
                )}
              </Td>
              <Td className="whitespace-nowrap text-text-muted">
                {key.expires_at === null || key.expires_at === undefined
                  ? 'Never'
                  : formatDateTime(
                      new Date(key.expires_at * 1000).toISOString()
                    )}
              </Td>
              <Td className="text-right">
                {status === 'revoked' ? null : (
                  <Button
                    variant="danger"
                    size="sm"
                    aria-label={`Revoke ${key.name}`}
                    onClick={() => {
                      onRevoke(key);
                    }}
                  >
                    Revoke
                  </Button>
                )}
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

/** The form that mints a key: a name, an expiry and optionally narrower scopes. */
function CreateKeyDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (key: ApiKeyCreated) => void;
}): React.ReactElement {
  const { getAccessToken } = useAuth();
  const held = scopesFromToken(getAccessToken());
  const [name, setName] = useState('');
  const [days, setDays] = useState(DEFAULT_EXPIRY_DAYS);
  const [restrict, setRestrict] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  const create = useMutationWithRefetch(
    (body: Parameters<typeof api.createApiKey>[0]) => api.createApiKey(body),
    API_KEYS_KEY
  );

  const trimmed = name.trim();
  const canSubmit = trimmed !== '' && (!restrict || chosen.length > 0);

  const submit = async (): Promise<void> => {
    try {
      const key = await create.mutate({
        name: trimmed,
        expires_at: expiryFrom(days),
        scopes: restrict ? chosen : null,
      });
      onCreated(key);
    } catch {
      return;
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Create an API key"
      description="The key acts as you. Its secret is shown once, right after it is created."
    >
      <form
        aria-label="Create an API key"
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) {
            void submit();
          }
        }}
      >
        <Field
          label="Name"
          hint="What the key is for, so you recognise it later."
        >
          {(control) => (
            <input
              {...control}
              value={name}
              maxLength={NAME_MAX}
              onChange={(event) => {
                setName(event.target.value);
              }}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field
          label="Expiration"
          hint="Keys always expire, at most a year out."
        >
          {(control) => (
            <select
              {...control}
              value={days}
              onChange={(event) => {
                setDays(Number(event.target.value));
              }}
              className={INPUT_CLASS}
            >
              {EXPIRY_CHOICES.map((choice) => (
                <option key={choice} value={choice}>
                  {`${String(choice)} days`}
                </option>
              ))}
            </select>
          )}
        </Field>
        <fieldset className="space-y-2 text-sm">
          <legend className="text-text-muted">Scopes</legend>
          <label className="flex items-center gap-2 text-text">
            <input
              type="radio"
              name="scope-mode"
              checked={!restrict}
              onChange={() => {
                setRestrict(false);
              }}
              className="size-3.5 accent-accent"
            />
            Everything you can do
          </label>
          {held.length === 0 ? null : (
            <label className="flex items-center gap-2 text-text">
              <input
                type="radio"
                name="scope-mode"
                checked={restrict}
                onChange={() => {
                  setRestrict(true);
                }}
                className="size-3.5 accent-accent"
              />
              Only the scopes I choose
            </label>
          )}
          {restrict ? (
            <div className="grid grid-cols-2 gap-1.5 pl-5">
              {held.map((scope) => (
                <label
                  key={scope}
                  className="flex items-center gap-2 font-mono text-xs text-text"
                >
                  <input
                    type="checkbox"
                    checked={chosen.includes(scope)}
                    onChange={(event) => {
                      setChosen((current) =>
                        event.target.checked
                          ? [...current, scope]
                          : current.filter((value) => value !== scope)
                      );
                    }}
                    className="size-3.5 accent-accent"
                  />
                  {scope}
                </label>
              ))}
            </div>
          ) : null}
        </fieldset>
        <ErrorNotice error={create.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            disabled={!canSubmit}
            busy={create.isMutating}
            busyLabel="Creating"
          >
            Create API key
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** The confirmation before a key stops working. */
function RevokeKeyDialog({
  apiKey,
  onClose,
}: {
  apiKey: ApiKey;
  onClose: () => void;
}): React.ReactElement {
  const revoke = useMutationWithRefetch(
    (keyId: string) => api.revokeApiKey(keyId),
    API_KEYS_KEY
  );

  const run = async (): Promise<void> => {
    try {
      await revoke.mutate(apiKey.key_id);
      onClose();
    } catch {
      return;
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Revoke API key"
      description="Anything using this key stops working on its next request. This cannot be undone."
    >
      <div className="space-y-4">
        <p className="text-sm text-text">
          {'Revoke '}
          <span className="font-medium text-text-strong">{apiKey.name}</span>
          <span className="font-mono text-xs text-text-muted">{` (${apiKey.prefix}...)`}</span>
          ?
        </p>
        <ErrorNotice error={revoke.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            busy={revoke.isMutating}
            busyLabel="Revoking"
            onClick={() => {
              void run();
            }}
          >
            Revoke key
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
