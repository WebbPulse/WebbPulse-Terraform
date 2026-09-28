/** The engine and engine version pair, with a way out to any exact release. */

import { useState } from 'react';

import type { Engine } from '../../api';
import { Field, INPUT_CLASS } from '../../components';
import {
  ENGINES,
  ENGINE_LABELS,
  ENGINE_VERSIONS,
  defaultEngineVersion,
  engineVersionProblem,
  isListedVersion,
} from './engineVersions';

/** The select value that switches the version to a typed one. */
const OTHER = '__other__';

/** An engine and the version it runs. */
export interface EngineChoice {
  engine: Engine;
  version: string;
}

/** Props for {@link EngineFields}. */
export interface EngineFieldsProps {
  value: EngineChoice;
  onChange: (value: EngineChoice) => void;
}

/**
 * The engine select and a select of its known versions.
 *
 * "Other version" swaps in a text input, so a version the list does not carry,
 * such as a newer patch, can still be pinned. A stored version off the list
 * opens in that mode.
 */
export function EngineFields({
  value,
  onChange,
}: EngineFieldsProps): React.ReactElement {
  const [typing, setTyping] = useState(
    () => !isListedVersion(value.engine, value.version)
  );
  const problem = typing ? engineVersionProblem(value.version) : null;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Engine">
        {(control) => (
          <select
            {...control}
            value={value.engine}
            onChange={(event) => {
              const engine = event.target.value as Engine;
              setTyping(false);
              onChange({ engine, version: defaultEngineVersion(engine) });
            }}
            className={INPUT_CLASS}
          >
            {ENGINES.map((engine) => (
              <option key={engine} value={engine}>
                {ENGINE_LABELS[engine]}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field
        label="Engine version"
        hint={
          typing
            ? undefined
            : value.version === defaultEngineVersion(value.engine)
              ? 'The newest release the runner ships with, so runs start without a download.'
              : 'Runs install this release before they start.'
        }
      >
        {(control) => (
          <select
            {...control}
            value={typing ? OTHER : value.version}
            onChange={(event) => {
              if (event.target.value === OTHER) {
                setTyping(true);
                return;
              }
              setTyping(false);
              onChange({ ...value, version: event.target.value });
            }}
            className={`${INPUT_CLASS} font-mono`}
          >
            {ENGINE_VERSIONS[value.engine].map((version, index) => (
              <option key={version} value={version}>
                {index === 0 ? `${version} (default)` : version}
              </option>
            ))}
            <option value={OTHER}>Other version</option>
          </select>
        )}
      </Field>
      {typing ? (
        <Field
          label="Specific version"
          className="sm:col-start-2"
          hint={
            problem === null
              ? 'An exact release, installed per run.'
              : undefined
          }
          error={value.version.trim() === '' ? null : problem}
        >
          {(control) => (
            <input
              {...control}
              value={value.version}
              placeholder={defaultEngineVersion(value.engine)}
              onChange={(event) => {
                onChange({ ...value, version: event.target.value });
              }}
              className={`${INPUT_CLASS} font-mono`}
            />
          )}
        </Field>
      ) : null}
    </div>
  );
}
