/** The variables tab: create, edit and delete, with sensitive values write-only. */

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

/** Props for {@link VariablesTab}. */
export interface VariablesTabProps {
  workspaceId: string;
}

/** The categories a variable can be in. */
const CATEGORIES: readonly VariableCategory[] = ['terraform', 'env'];

/** The variables table and the form that writes one. */
export function VariablesTab({
  workspaceId,
}: VariablesTabProps): React.ReactElement {
  const auth = useQueryAuth();
  const queryKey = `variables:${workspaceId}`;
  const query = usePolledQuery<VariableList>(
    ({ signal }) => api.listVariables(workspaceId, { signal }),
    { intervalMs: 60_000, queryKey, auth }
  );
  const [editing, setEditing] = useState<Variable | null>(null);

  return (
    <div className="space-y-5">
      <ErrorNotice error={query.error} />
      <VariableForm
        workspaceId={workspaceId}
        queryKey={queryKey}
        editing={editing}
        onDone={() => {
          setEditing(null);
        }}
      />
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading variables" className="size-4" />
          Loading variables
        </div>
      ) : (
        <VariableTable
          workspaceId={workspaceId}
          queryKey={queryKey}
          variables={query.data?.items ?? []}
          onEdit={setEditing}
        />
      )}
    </div>
  );
}

/** The table of variables, each row with an edit and a delete. */
function VariableTable({
  workspaceId,
  queryKey,
  variables,
  onEdit,
}: {
  workspaceId: string;
  queryKey: string;
  variables: Variable[];
  onEdit: (variable: Variable) => void;
}): React.ReactElement {
  const { mutate, error } = useMutationWithRefetch(
    (key: string) => api.deleteVariable(workspaceId, key),
    queryKey
  );

  if (variables.length === 0) {
    return (
      <EmptyState
        title="No variables yet."
        hint="Terraform variables become -var values; env variables are set on the runner."
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
              <Td className="font-mono text-xs text-text-strong">
                {variable.key}
              </Td>
              <Td className="max-w-md">
                {variable.sensitive ? (
                  <span className="inline-flex items-center gap-1.5 text-xs text-text-faint">
                    <svg
                      aria-hidden="true"
                      viewBox="0 0 16 16"
                      className="size-3.5"
                      fill="none"
                    >
                      <rect
                        x="3.5"
                        y="7"
                        width="9"
                        height="6.5"
                        rx="1.5"
                        stroke="currentColor"
                        strokeWidth="1.4"
                      />
                      <path
                        d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"
                        stroke="currentColor"
                        strokeWidth="1.4"
                      />
                    </svg>
                    Write only
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
                  className="text-rose-300 hover:text-rose-200"
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
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [category, setCategory] = useState<VariableCategory>('terraform');
  const [sensitive, setSensitive] = useState(false);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  const editingKey = editing?.key ?? null;
  if (editingKey !== loadedFor) {
    setLoadedFor(editingKey);
    setKey(editing?.key ?? '');
    setValue(editing?.sensitive === true ? '' : (editing?.value ?? ''));
    setCategory(editing?.category ?? 'terraform');
    setSensitive(editing?.sensitive ?? false);
  }

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.putVariable(workspaceId, key, {
        value,
        category,
        sensitive,
      }),
    queryKey
  );

  const submit = async (): Promise<void> => {
    try {
      await mutate();
      setKey('');
      setValue('');
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
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold text-text-strong">
          {editing === null ? 'Add a variable' : `Edit ${editing.key}`}
        </h2>
        {sensitive ? (
          <span className="text-xs text-text-faint">
            Sensitive values are stored write only and never shown again.
          </span>
        ) : null}
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)_8rem]">
        <Field label="Key">
          {(control) => (
            <input
              {...control}
              required
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
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-text-muted">
          <input
            type="checkbox"
            checked={sensitive}
            onChange={(event) => {
              setSensitive(event.target.checked);
            }}
            className="size-3.5 accent-brand-500"
          />
          Sensitive
        </label>
        <span className="flex-1" />
        {editing === null ? null : (
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        )}
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
