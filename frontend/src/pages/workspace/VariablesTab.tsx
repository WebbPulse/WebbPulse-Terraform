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
import { ErrorNotice, Spinner } from '../../components';

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
        <Spinner label="Loading variables" />
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
    return <p className="text-surface-300">No variables yet.</p>;
  }
  return (
    <div className="space-y-2">
      <ErrorNotice error={error} />
      <table className="w-full text-left text-sm">
        <thead className="text-surface-400">
          <tr>
            <th className="py-2">Key</th>
            <th className="py-2">Value</th>
            <th className="py-2">Category</th>
            <th className="py-2" />
          </tr>
        </thead>
        <tbody>
          {variables.map((variable) => (
            <tr key={variable.key} className="border-t border-surface-700">
              <td className="py-2 font-mono">{variable.key}</td>
              <td className="py-2 text-surface-300">
                {variable.sensitive ? (
                  <span className="text-surface-400">Write only</span>
                ) : (
                  <span className="font-mono">{variable.value ?? ''}</span>
                )}
              </td>
              <td className="py-2 text-surface-300">{variable.category}</td>
              <td className="py-2 text-right">
                <button
                  type="button"
                  onClick={() => {
                    onEdit(variable);
                  }}
                  className="mr-3 text-brand-300 hover:text-brand-200"
                >
                  Edit
                </button>
                <button
                  type="button"
                  onClick={() => {
                    void mutate(variable.key).catch(() => undefined);
                  }}
                  className="text-rose-300 hover:text-rose-200"
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
      className="flex flex-wrap items-end gap-3 rounded-lg border border-surface-700 bg-surface-800 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="text-sm">
        <span className="block text-surface-300">Key</span>
        <input
          required
          value={key}
          readOnly={editing !== null}
          onChange={(event) => {
            setKey(event.target.value);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2 font-mono read-only:text-surface-400"
        />
      </label>
      <label className="text-sm">
        <span className="block text-surface-300">
          Value{sensitive ? ' (write only)' : ''}
        </span>
        <input
          required
          type={sensitive ? 'password' : 'text'}
          autoComplete="off"
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2 font-mono"
        />
      </label>
      <label className="text-sm">
        <span className="block text-surface-300">Category</span>
        <select
          value={category}
          onChange={(event) => {
            setCategory(event.target.value as VariableCategory);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2"
        >
          {CATEGORIES.map((value_) => (
            <option key={value_} value={value_}>
              {value_}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm text-surface-300">
        <input
          type="checkbox"
          checked={sensitive}
          onChange={(event) => {
            setSensitive(event.target.checked);
          }}
        />
        Sensitive
      </label>
      <button
        type="submit"
        disabled={isMutating}
        className="flex items-center gap-2 rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {isMutating ? <Spinner label="Saving the variable" /> : null}
        {editing === null ? 'Add variable' : 'Save variable'}
      </button>
      {editing === null ? null : (
        <button
          type="button"
          onClick={onDone}
          className="rounded-md border border-surface-600 px-3 py-2 text-sm text-surface-200"
        >
          Cancel
        </button>
      )}
      <ErrorNotice error={error} className="w-full" />
    </form>
  );
}
