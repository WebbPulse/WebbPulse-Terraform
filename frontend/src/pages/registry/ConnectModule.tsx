/**
 * Connects a module to a GitHub repository, like publishing a module from VCS.
 *
 * Pick a repository the App can reach, confirm the name and provider, and
 * choose whether the repository's existing version tags are imported now.
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type ModuleCreate } from '../../api';
import {
  Button,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  buttonClass,
} from '../../components';
import { RepositoryPicker } from '../workspace/vcs/RepositoryPicker';
import {
  MODULES_KEY,
  addressFromRepository,
  modulePagePath,
} from './moduleText';

const NAME_PATTERN = /^[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?$/;
const PROVIDER_PATTERN = /^[0-9a-z]{1,64}$/;

/** The connect module page. */
export function ConnectModule(): React.ReactElement {
  const navigate = useNavigate();
  const [repository, setRepository] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [provider, setProvider] = useState('');
  const [importTags, setImportTags] = useState(true);
  const create = useMutationWithRefetch(
    (body: ModuleCreate) => api.createModule(body),
    MODULES_KEY
  );

  const derived =
    repository === null ? null : addressFromRepository(repository);
  const effectiveName = name.trim() || derived?.name || '';
  const effectiveProvider = provider.trim() || derived?.provider || '';
  const nameError =
    effectiveName !== '' && !NAME_PATTERN.test(effectiveName)
      ? 'Letters, digits, hyphens and underscores, starting and ending with a letter or digit.'
      : null;
  const providerError =
    effectiveProvider !== '' && !PROVIDER_PATTERN.test(effectiveProvider)
      ? 'Lowercase letters and digits only, such as aws or null.'
      : null;
  const namespace = repository?.split('/')[0] ?? '';
  const ready =
    repository !== null &&
    effectiveName !== '' &&
    effectiveProvider !== '' &&
    nameError === null &&
    providerError === null;

  const submit = async (): Promise<void> => {
    if (repository === null) {
      return;
    }
    const body: ModuleCreate = {
      vcs_repo: repository,
      import_tags: importTags,
    };
    if (name.trim() !== '') {
      body.name = name.trim();
    }
    if (provider.trim() !== '') {
      body.provider = provider.trim();
    }
    try {
      const module = await create.mutate(body);
      void navigate(modulePagePath(module));
    } catch {
      return;
    }
  };

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        title="Connect a module"
        crumbs={[{ label: 'Registry', to: '/registry' }]}
        description="Each vX.Y.Z tag pushed to the repository publishes a version of the module."
      />
      <section className="space-y-3 rounded-lg border border-line bg-panel p-5">
        <h2 className="text-sm font-medium text-text-strong">
          1. Choose a repository
        </h2>
        <p className="text-xs text-text-muted">
          The module is packed from the repository root at each tag. A
          repository named{' '}
          <span className="font-mono">terraform-provider-name</span> fills in
          the name and provider.
        </p>
        <RepositoryPicker
          value={repository}
          onChange={(picked) => {
            setRepository(picked.full_name);
          }}
        />
      </section>
      {repository === null ? null : (
        <section className="space-y-4 rounded-lg border border-line bg-panel p-5">
          <h2 className="text-sm font-medium text-text-strong">
            2. Confirm the module address
          </h2>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Module name"
              hint={
                derived === null
                  ? 'Required, since the repository name implies none.'
                  : 'Leave empty to use the name the repository implies.'
              }
              error={nameError}
            >
              {(control) => (
                <input
                  {...control}
                  value={name}
                  placeholder={derived?.name ?? 'network'}
                  onChange={(event) => {
                    setName(event.target.value);
                  }}
                  className={`${INPUT_CLASS} font-mono`}
                />
              )}
            </Field>
            <Field
              label="Provider"
              hint={
                derived === null
                  ? 'The main provider the module uses, such as aws.'
                  : 'Leave empty to use the provider the repository implies.'
              }
              error={providerError}
            >
              {(control) => (
                <input
                  {...control}
                  value={provider}
                  placeholder={derived?.provider ?? 'aws'}
                  onChange={(event) => {
                    setProvider(event.target.value);
                  }}
                  className={`${INPUT_CLASS} font-mono`}
                />
              )}
            </Field>
          </div>
          <p className="text-xs text-text-muted">
            {'Source address '}
            <span
              data-testid="connect-address"
              className="font-mono text-text-strong"
            >
              {namespace}/{effectiveName || '<name>'}/
              {effectiveProvider || '<provider>'}
            </span>
          </p>
          <label className="flex items-start gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={importTags}
              onChange={(event) => {
                setImportTags(event.target.checked);
              }}
              className="mt-0.5 accent-accent"
            />
            <span>
              Import existing version tags
              <span className="block text-xs text-text-faint">
                Tags already in the repository publish now. Otherwise only tags
                pushed from here on do, until a resync.
              </span>
            </span>
          </label>
          <ErrorNotice error={create.error} />
          <div className="flex justify-end gap-2">
            <Link to="/registry" className={buttonClass('ghost')}>
              Cancel
            </Link>
            <Button
              variant="primary"
              disabled={!ready}
              busy={create.isMutating}
              busyLabel="Connecting module"
              onClick={() => {
                void submit();
              }}
            >
              Connect module
            </Button>
          </div>
        </section>
      )}
    </div>
  );
}
