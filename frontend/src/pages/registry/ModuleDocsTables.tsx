/** The inputs, outputs, resources and providers of a module version, as tables. */

import type {
  ModuleInput,
  ModuleOutput,
  ModuleProvider,
  ModuleResource,
} from '../../api';
import { Table, Td, Th, Tr } from '../../components';

/** The line a table shows when a list is empty. */
function Nothing({ children }: { children: string }): React.ReactElement {
  return (
    <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
      {children}
    </p>
  );
}

/** A small pill beside a name. */
function Tag({
  children,
  tone = 'neutral',
}: {
  children: string;
  tone?: 'neutral' | 'warning';
}): React.ReactElement {
  const classes =
    tone === 'warning'
      ? 'border-warning-line bg-warning-soft text-warning'
      : 'border-line bg-raised text-text-muted';
  return (
    <span
      className={`rounded-full border px-1.5 py-px text-[10px] font-medium ${classes}`}
    >
      {children}
    </span>
  );
}

/** The inputs, required ones first, with type, default and description. */
export function InputsTable({
  inputs,
}: {
  inputs: readonly ModuleInput[];
}): React.ReactElement {
  if (inputs.length === 0) {
    return <Nothing>This module takes no inputs.</Nothing>;
  }
  return (
    <Table label="Inputs">
      <thead>
        <tr>
          <Th>Name</Th>
          <Th>Type</Th>
          <Th>Default</Th>
          <Th>Description</Th>
        </tr>
      </thead>
      <tbody>
        {inputs.map((input) => (
          <Tr key={input.name}>
            <Td>
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-xs text-text-strong">
                  {input.name}
                </span>
                {input.required ? <Tag tone="warning">required</Tag> : null}
                {input.sensitive === true ? <Tag>sensitive</Tag> : null}
              </span>
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              <pre className="whitespace-pre-wrap">{input.type ?? 'any'}</pre>
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {input.default === null || input.default === undefined ? (
                <span className="font-sans text-text-faint">None</span>
              ) : (
                <pre className="max-w-xs overflow-x-auto whitespace-pre">
                  {input.default}
                </pre>
              )}
            </Td>
            <Td className="text-sm text-text">{input.description ?? ''}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/** The outputs with their descriptions. */
export function OutputsTable({
  outputs,
}: {
  outputs: readonly ModuleOutput[];
}): React.ReactElement {
  if (outputs.length === 0) {
    return <Nothing>This module has no outputs.</Nothing>;
  }
  return (
    <Table label="Outputs">
      <thead>
        <tr>
          <Th>Name</Th>
          <Th>Description</Th>
        </tr>
      </thead>
      <tbody>
        {outputs.map((output) => (
          <Tr key={output.name}>
            <Td>
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-xs text-text-strong">
                  {output.name}
                </span>
                {output.sensitive === true ? <Tag>sensitive</Tag> : null}
              </span>
            </Td>
            <Td className="text-sm text-text">{output.description ?? ''}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/** The resources the module declares, by type and name. */
export function ResourcesTable({
  resources,
}: {
  resources: readonly ModuleResource[];
}): React.ReactElement {
  if (resources.length === 0) {
    return <Nothing>This module declares no resources.</Nothing>;
  }
  return (
    <Table label="Resources">
      <thead>
        <tr>
          <Th>Type</Th>
          <Th>Name</Th>
        </tr>
      </thead>
      <tbody>
        {resources.map((resource) => (
          <Tr key={`${resource.type}.${resource.name}`}>
            <Td className="font-mono text-xs text-text-strong">
              {resource.type}
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {resource.name}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/** The provider requirements with source and version constraint. */
export function ProvidersTable({
  providers,
}: {
  providers: readonly ModuleProvider[];
}): React.ReactElement {
  if (providers.length === 0) {
    return <Nothing>This module declares no provider requirements.</Nothing>;
  }
  return (
    <Table label="Providers">
      <thead>
        <tr>
          <Th>Name</Th>
          <Th>Source</Th>
          <Th>Version</Th>
        </tr>
      </thead>
      <tbody>
        {providers.map((provider) => (
          <Tr key={provider.name}>
            <Td className="font-mono text-xs text-text-strong">
              {provider.name}
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {provider.source ?? ''}
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {provider.version ?? 'any'}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}
