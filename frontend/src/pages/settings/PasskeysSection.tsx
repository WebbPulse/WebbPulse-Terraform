/** The Passkeys section of the security page: list, add, rename and remove. */

import type { AuthClient, Passkey } from '@webbpulse/auth';
import { usePasskeyPanel, type PasskeyPanel } from '@webbpulse/auth/panels';

import {
  Button,
  EmptyState,
  INPUT_CLASS,
  RelativeTime,
  Spinner,
} from '../../components';

/** Props for {@link PasskeysSection}. */
export interface PasskeysSectionProps {
  client: AuthClient<unknown>;
}

/** One passkey: its name and dates, or the rename form while it is open. */
function PasskeyRow({
  passkey,
  panel,
}: {
  passkey: Passkey;
  panel: PasskeyPanel;
}): React.ReactElement {
  if (panel.renaming === passkey.credentialId) {
    return (
      <li className="px-3 py-2">
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void panel.commitRename();
          }}
        >
          <input
            aria-label="Passkey name"
            className={`${INPUT_CLASS} h-8 flex-1`}
            value={panel.draftRename}
            autoFocus
            maxLength={64}
            disabled={panel.busy}
            onChange={(event) => {
              panel.setDraftRename(event.target.value);
            }}
          />
          <Button type="submit" size="sm" variant="primary" busy={panel.busy}>
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={panel.busy}
            onClick={panel.cancelRename}
          >
            Cancel
          </Button>
        </form>
      </li>
    );
  }
  return (
    <li
      data-testid="passkey-row"
      className="flex items-center gap-3 px-3 py-2 text-sm"
    >
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-text-strong">{passkey.name}</p>
        <p className="text-xs text-text-faint">
          Added <RelativeTime iso={passkey.createdAt} />
          {passkey.lastUsedAt === undefined ? null : (
            <>
              {' · Last used '}
              <RelativeTime iso={passkey.lastUsedAt} />
            </>
          )}
        </p>
      </div>
      <div className="flex shrink-0 gap-1">
        <Button
          size="sm"
          variant="ghost"
          disabled={panel.busy}
          onClick={() => {
            panel.startRename(passkey);
          }}
        >
          Rename
        </Button>
        <Button
          size="sm"
          variant="danger"
          disabled={panel.busy}
          aria-label={`Remove ${passkey.name}`}
          onClick={() => void panel.remove(passkey.credentialId)}
        >
          Remove
        </Button>
      </div>
    </li>
  );
}

/** The passkey list with its add form, hidden where passkeys are off. */
export function PasskeysSection({
  client,
}: PasskeysSectionProps): React.ReactElement | null {
  const panel = usePasskeyPanel({
    client,
    messages: {
      created: (passkey) => `Passkey "${passkey.name}" added.`,
      renamed: 'Passkey renamed.',
      removed: 'Passkey removed.',
    },
  });

  if (panel.unavailable) {
    return null;
  }

  return (
    <section
      aria-labelledby="passkeys-heading"
      className="space-y-3 rounded-lg border border-line bg-panel p-4"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2
            id="passkeys-heading"
            className="text-sm font-medium text-text-strong"
          >
            Passkeys
          </h2>
          <p className="mt-1 text-sm text-text-muted">
            Sign in with your device, a security key or a password manager
            instead of a password.
          </p>
        </div>
        {panel.supported && !panel.adding ? (
          <Button
            size="sm"
            variant="primary"
            disabled={panel.busy}
            onClick={panel.startCreate}
          >
            Add a passkey
          </Button>
        ) : null}
      </div>
      {panel.error === null ? null : (
        <p
          role="alert"
          className="rounded-md border border-danger-line bg-danger-soft px-3 py-2 text-sm text-danger"
        >
          {panel.error}
        </p>
      )}
      {panel.notice === null ? null : (
        <p
          role="status"
          className="rounded-md border border-line bg-accent-soft px-3 py-2 text-sm text-text-strong"
        >
          {panel.notice}
        </p>
      )}
      {panel.supported ? null : (
        <p className="text-xs text-text-faint">
          This browser cannot create passkeys. Passkeys added elsewhere are
          listed here and can still be removed.
        </p>
      )}
      {panel.adding ? (
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void panel.commitCreate();
          }}
        >
          <input
            aria-label="New passkey name"
            placeholder="Name this passkey"
            className={`${INPUT_CLASS} h-8 flex-1`}
            value={panel.draftName}
            autoFocus
            maxLength={64}
            disabled={panel.busy}
            onChange={(event) => {
              panel.setDraftName(event.target.value);
            }}
          />
          <Button
            type="submit"
            size="sm"
            variant="primary"
            busy={panel.busy}
            busyLabel="Waiting for your passkey"
          >
            Continue
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={panel.busy}
            onClick={panel.cancelCreate}
          >
            Cancel
          </Button>
        </form>
      ) : null}
      {panel.items === null ? (
        panel.loading ? (
          <Spinner label="Loading passkeys" className="size-4" />
        ) : null
      ) : panel.items.length === 0 ? (
        <EmptyState
          title="No passkeys on this account yet."
          hint="Add one to sign in without typing a password."
        />
      ) : (
        <ul
          aria-label="Passkeys"
          className="divide-y divide-line rounded-md border border-line"
        >
          {panel.items.map((passkey) => (
            <PasskeyRow
              key={passkey.credentialId}
              passkey={passkey}
              panel={panel}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
