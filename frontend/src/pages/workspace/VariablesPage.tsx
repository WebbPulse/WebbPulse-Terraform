/** The variables page: the table, and an inline form that adds or edits one. */

import { useState } from 'react';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type Variable,
  type VariableCategory,
  type VariableList,
} from '../../api';
import {
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
} from '../../components';
import { useWorkspace } from '../workspaceContext';

/** The categories a variable can be in. */
const CATEGORIES: readonly VariableCategory[] = ['terraform', 'env'];

/** The variables page. */
export function VariablesPage(): React.ReactElement {
  const { workspace } = useWorkspace();
  const workspaceId = workspace.workspace_id;
  const auth = useQueryAuth();
  const queryKey = `variables:${workspaceId}`;
  const query = usePolledQuery<VariableList>(
    ({ signal }) => api.listVariables(workspaceId, { signal }),
    { intervalMs: 60_000, queryKey, auth }
  );
  const [form, setForm] = useState<{ editing: Variable | null } | null>(null);
  const variables = query.data?.items ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-sm font-semibold text-text-strong">Variables</h2>
        <p className="mt-1 max-w-prose text-sm text-text-muted">
          Terraform variables become -var values on every run. Environment
          variables are set on the runner before the engine starts.
        </p>
      </div>
      <p className="rounded-md border border-line bg-panel px-3 py-2 text-xs text-text-muted">
        Sensitive variables are write only. Their values are never shown again
        and must be entered anew on every edit.
      </p>
      <ErrorNotice error={query.error} />
      <section aria-labelledby="workspace-variables" className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h3
            id="workspace-variables"
            className="text-sm font-medium text-text-strong"
          >
            Workspace variables{' '}
            <span className="text-text-faint">({variables.length})</span>
          </h3>
          {form === null ? (
            <Button
              size="sm"
              onClick={() => {
                setForm({ editing: null });
              }}
            >
              + Add variable
            </Button>
          ) : null}
        </div>
        {form === null ? null : (
          <VariableForm
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
            <Spinner label="Loading variables" className="size-4" />
            Loading variables
          </div>
        ) : (
          <VariableTable
            workspaceId={workspaceId}
            queryKey={queryKey}
            variables={variables}
            onEdit={(variable) => {
              setForm({ editing: variable });
            }}
            onAdd={() => {
              setForm({ editing: null });
            }}
          />
        )}
      </section>
    </div>
  );
}

/** The table of variables, each row with an edit and a delete. */
function VariableTable({
  workspaceId,
  queryKey,
  variables,
  onEdit,
  onAdd,
}: {
  workspaceId: string;
  queryKey: string;
  variables: Variable[];
  onEdit: (variable: Variable) => void;
  onAdd: () => void;
}): React.ReactElement {
  const { mutate, error } = useMutationWithRefetch(
    (key: string) => api.deleteVariable(workspaceId, key),
    queryKey
  );

  if (variables.length === 0) {
    return (
      <EmptyState
        title="No variables yet."
        hint="Add one to pass a value into every run."
        action={
          <Button variant="primary" onClick={onAdd}>
            + Add variable
          </Button>
        }
      />
    );
  }
  return (
    <div className="space-y-2">
      <ErrorNotice error={error} />
      <Table label="Variables">
        <thead>
          <tr>
            <Th>Key</Th>
            <Th>Value</Th>
            <Th>Category</Th>
            <Th className="text-right">Actions</Th>
          </tr>
        </thead>
        <tbody>
          {variables.map((variable) => (
            <Tr key={variable.key}>
              <Td>
                <span className="inline-flex items-center gap-2 font-mono text-xs text-text-strong">
                  {variable.key}
                  {variable.sensitive ? (
                    <span className="rounded border border-line-strong px-1 font-sans text-[10px] text-text-faint">
                      Sensitive
                    </span>
                  ) : null}
                </span>
                {(variable.description ?? '') === '' ? null : (
                  <p className="mt-0.5 max-w-md truncate text-xs text-text-faint">
                    {variable.description}
                  </p>
                )}
              </Td>
              <Td className="max-w-md">
                {variable.sensitive ? (
                  <span className="text-xs text-text-faint">
                    Sensitive, write only
                  </span>
                ) : (
                  <span className="block truncate font-mono text-xs text-text">
                    {variable.value ?? ''}
                  </span>
                )}
              </Td>
              <Td className="text-xs text-text-muted">{variable.category}</Td>
              <Td className="text-right whitespace-nowrap">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    onEdit(variable);
                  }}
                >
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-danger hover:underline"
                  onClick={() => {
                    void mutate(variable.key).catch(() => undefined);
                  }}
                >
                  Delete
                </Button>
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

/**
 * The form that writes one variable.
 *
 * Editing a sensitive variable starts with an empty value box, because the API
 * never returns the stored one: a save re-enters it rather than round-tripping
 * a value the browser was never given.
 */
function VariableForm({
  workspaceId,
  queryKey,
  editing,
  onDone,
}: {
  workspaceId: string;
  queryKey: string;
  editing: Variable | null;
  onDone: () => void;
}): React.ReactElement {
  const [key, setKey] = useState(editing?.key ?? '');
  const [value, setValue] = useState(
    editing?.sensitive === true ? '' : (editing?.value ?? '')
  );
  const [description, setDescription] = useState(editing?.description ?? '');
  const [category, setCategory] = useState<VariableCategory>(
    editing?.category ?? 'terraform'
  );
  const [sensitive, setSensitive] = useState(editing?.sensitive ?? false);

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.putVariable(workspaceId, key, {
        value,
        category,
        hcl: category === 'terraform' && editing?.hcl === true,
        sensitive,
        description,
      }),
    queryKey
  );

  const submit = async (): Promise<void> => {
    try {
      await mutate();
      onDone();
    } catch {
      return;
    }
  };

  return (
    <form
      aria-label={editing === null ? 'Create a variable' : 'Edit the variable'}
      className="space-y-3 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h4 className="text-sm font-semibold text-text-strong">
        {editing === null ? 'Add a variable' : `Edit ${editing.key}`}
      </h4>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)_8rem]">
        <Field label="Key">
          {(control) => (
            <input
              {...control}
              required
              autoFocus={editing === null}
              value={key}
              readOnly={editing !== null}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => {
                setKey(event.target.value);
              }}
              className={`${INPUT_CLASS} font-mono`}
            />
          )}
        </Field>
        <Field label={sensitive ? 'Value (write only)' : 'Value'}>
          {(control) => (
            <input
              {...control}
              required
              type={sensitive ? 'password' : 'text'}
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(event) => {
                setValue(event.target.value);
              }}
              className={`${INPUT_CLASS} font-mono`}
            />
          )}
        </Field>
        <Field label="Category">
          {(control) => (
            <select
              {...control}
              value={category}
              onChange={(event) => {
                setCategory(event.target.value as VariableCategory);
              }}
              className={INPUT_CLASS}
            >
              {CATEGORIES.map((value_) => (
                <option key={value_} value={value_}>
                  {value_}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      <Field label="Description" hint="Optional.">
        {(control) => (
          <input
            {...control}
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-text-muted">
          <input
            type="checkbox"
            checked={sensitive}
            onChange={(event) => {
              setSensitive(event.target.checked);
            }}
            className="size-3.5 accent-accent"
          />
          Sensitive
        </label>
        <span className="flex-1" />
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving the variable"
        >
          {editing === null ? 'Add variable' : 'Save variable'}
        </Button>
      </div>
      <ErrorNotice error={error} />
    </form>
  );
}
