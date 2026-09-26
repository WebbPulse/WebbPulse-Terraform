/** The branch, working directory and trigger settings of a connected repository. */

import { useId } from 'react';

import { Field, INPUT_CLASS } from '../../../components';
import type { VcsSettings } from './vcsSettings';

/** Props for {@link VcsFields}. */
export interface VcsFieldsProps {
  value: VcsSettings;
  onChange: (value: VcsSettings) => void;
  /** The repository's default branch, shown as the branch placeholder. */
  defaultBranch: string | null;
}

/** The fields under a picked repository. */
export function VcsFields({
  value,
  onChange,
  defaultBranch,
}: VcsFieldsProps): React.ReactElement {
  const set = (patch: Partial<VcsSettings>): void => {
    onChange({ ...value, ...patch });
  };
  const radioName = useId();
  const directory = value.workingDirectory
    .trim()
    .replace(/^\.(\/|$)/, '')
    .replace(/\/+$/, '');
  return (
    <div className="space-y-4">
      <Field
        label="VCS branch"
        hint={
          defaultBranch
            ? `Pushes to this branch start runs. Leave empty for the default branch, ${defaultBranch}.`
            : "Pushes to this branch start runs. Leave empty for the repository's default branch."
        }
      >
        {(control) => (
          <input
            {...control}
            value={value.branch}
            placeholder={defaultBranch ?? 'default branch'}
            onChange={(event) => {
              set({ branch: event.target.value });
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      <Field
        label="Working directory"
        hint="The directory Terraform runs in, relative to the repository root. Leave empty for the root."
      >
        {(control) => (
          <input
            {...control}
            value={value.workingDirectory}
            placeholder="."
            onChange={(event) => {
              set({ workingDirectory: event.target.value });
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      <fieldset className="space-y-2 text-sm">
        <legend className="text-text-muted">Automatic run triggering</legend>
        <label className="flex items-start gap-2">
          <input
            type="radio"
            name={radioName}
            aria-label="Always trigger runs"
            checked={value.triggerMode === 'always'}
            onChange={() => {
              set({ triggerMode: 'always' });
            }}
            className="mt-0.5 accent-accent"
          />
          <span>
            <span className="text-text">Always trigger runs</span>
            <span className="block text-xs text-text-muted">
              Every push to the branch starts a run, whatever it changed.
            </span>
          </span>
        </label>
        <label className="flex items-start gap-2">
          <input
            type="radio"
            name={radioName}
            aria-label="Only trigger runs when files in specified paths change"
            checked={value.triggerMode === 'paths'}
            onChange={() => {
              set({ triggerMode: 'paths' });
            }}
            className="mt-0.5 accent-accent"
          />
          <span>
            <span className="text-text">
              Only trigger runs when files in specified paths change
            </span>
            <span className="block text-xs text-text-muted">
              Glob patterns, one per line.
            </span>
          </span>
        </label>
        {value.triggerMode === 'paths' ? (
          <Field
            label="Trigger patterns"
            className="pl-6"
            hint={`Leave empty to trigger on changes under ${directory === '' ? 'the repository root' : `${directory}/`}.`}
          >
            {(control) => (
              <textarea
                {...control}
                rows={3}
                value={value.patterns}
                placeholder={directory === '' ? '**' : `${directory}/**`}
                onChange={(event) => {
                  set({ patterns: event.target.value });
                }}
                className={`${INPUT_CLASS} h-auto py-1.5 font-mono`}
              />
            )}
          </Field>
        ) : null}
      </fieldset>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          aria-label="Automatic speculative plans"
          checked={value.speculativePlans}
          onChange={(event) => {
            set({ speculativePlans: event.target.checked });
          }}
          className="mt-0.5 accent-accent"
        />
        <span>
          <span className="text-text">Automatic speculative plans</span>
          <span className="block text-xs text-text-muted">
            Pull requests get a plan-only run, which can never apply.
          </span>
        </span>
      </label>
    </div>
  );
}
