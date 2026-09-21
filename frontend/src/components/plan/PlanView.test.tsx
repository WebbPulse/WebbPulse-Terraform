import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import {
  aResourceChange,
  aRunPlan,
  anEmptyRunPlan,
} from '../../test-helpers/fixtures';
import { PlanView } from './PlanView';

/** The row for one resource address. */
function rowFor(address: string): HTMLElement {
  const row = screen
    .getByTestId('resource-changes')
    .querySelector<HTMLElement>(`[data-address="${address}"]`);
  if (row === null) {
    throw new Error(`No row for ${address}.`);
  }
  return row;
}

/** Opens a resource row's diff. */
async function openRow(address: string): Promise<HTMLElement> {
  const row = rowFor(address);
  await userEvent.click(within(row).getByRole('button'));
  return row;
}

describe('PlanView', () => {
  it('counts the changes, replacements apart', () => {
    render(<PlanView plan={aRunPlan()} />);

    const summary = screen.getByTestId('plan-summary-line');
    expect(summary).toHaveTextContent('2 to add');
    expect(summary).toHaveTextContent('1 to change');
    expect(summary).toHaveTextContent('1 to destroy');
    expect(screen.getByTestId('plan-replace-count')).toHaveTextContent(
      '1 to replace'
    );
  });

  it('says so when a plan has nothing to do', () => {
    render(<PlanView plan={anEmptyRunPlan()} />);

    expect(screen.getByTestId('plan-summary-line')).toHaveTextContent(
      'No changes. Your infrastructure matches the configuration.'
    );
    expect(screen.queryByTestId('resource-changes')).not.toBeInTheDocument();
  });

  it('names the version the plan was made with', () => {
    render(<PlanView plan={aRunPlan()} />);

    expect(screen.getByText('1.11.0')).toBeInTheDocument();
  });

  it('lists one row per resource the plan touches', () => {
    render(<PlanView plan={aRunPlan()} />);

    expect(screen.getAllByTestId('resource-change')).toHaveLength(6);
    expect(rowFor('aws_s3_bucket.logs')).toHaveAttribute(
      'data-action',
      'create'
    );
    expect(rowFor('aws_db_instance.primary')).toHaveAttribute(
      'data-action',
      'replace'
    );
    expect(rowFor('data.aws_caller_identity.current')).toHaveAttribute(
      'data-action',
      'read'
    );
  });

  it('shows why a resource is being replaced', () => {
    render(<PlanView plan={aRunPlan()} />);

    expect(
      within(rowFor('aws_db_instance.primary')).getByTestId('action-reason')
    ).toHaveTextContent('replace because cannot update');
  });

  it('opens a create to its new attributes', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_s3_bucket.logs');
    const bucket = within(row)
      .getAllByTestId('attribute-row')
      .find((entry) => entry.dataset['attribute'] === 'bucket');
    expect(bucket).toHaveAttribute('data-kind', 'added');
    expect(bucket).toHaveTextContent('"platform-logs"');
  });

  it('shows an update as the old value becoming the new one', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_iam_role.runner');
    const duration = within(row)
      .getAllByTestId('attribute-row')
      .find((entry) => entry.dataset['attribute'] === 'max_session_duration');
    expect(duration).toHaveAttribute('data-kind', 'changed');
    expect(duration).toHaveTextContent('3600');
    expect(duration).toHaveTextContent('7200');
  });

  it('folds unchanged attributes behind their count', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_iam_role.runner');
    const fold = within(row).getByRole('button', {
      name: /unchanged attributes/,
    });
    expect(fold).toHaveTextContent('Show 2 unchanged attributes');
    expect(fold).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(fold);
    expect(fold).toHaveAttribute('aria-expanded', 'true');
    expect(
      within(row)
        .getAllByTestId('attribute-row')
        .filter((entry) => entry.dataset['kind'] === 'unchanged')
    ).toHaveLength(2);
  });

  it('flags the attribute that forces a replacement', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_db_instance.primary');
    expect(within(row).getByTestId('forces-replacement')).toHaveTextContent(
      'forces replacement'
    );
  });

  it('withholds a sensitive value', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_db_instance.primary');
    const password = within(row)
      .getAllByTestId('attribute-row')
      .find((entry) => entry.dataset['attribute'] === 'password');
    expect(password).toHaveTextContent('(sensitive value)');
    expect(password).not.toHaveTextContent('new');
  });

  it('marks a value only known once applied', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_s3_bucket.logs');
    const arn = within(row)
      .getAllByTestId('attribute-row')
      .find((entry) => entry.dataset['attribute'] === 'arn');
    expect(arn).toHaveTextContent('(known after apply)');
  });

  it('shows a delete as its attributes going away', async () => {
    render(<PlanView plan={aRunPlan()} />);

    const row = await openRow('aws_sqs_queue.old');
    const rows = within(row).getAllByTestId('attribute-row');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((entry) => entry.dataset['kind'] === 'removed')).toBe(
      true
    );
  });

  it('leaves an unchanged resource with nothing to open', () => {
    render(<PlanView plan={aRunPlan()} />);

    expect(
      within(rowFor('aws_kms_key.state')).getByRole('button')
    ).toBeDisabled();
  });

  it('lists the outputs, sensitive and unknown ones held back', () => {
    render(<PlanView plan={aRunPlan()} />);

    const outputs = screen.getByTestId('output-changes');
    expect(within(outputs).getAllByTestId('output-change')).toHaveLength(3);
    expect(outputs).toHaveTextContent('"platform-logs"');
    expect(outputs).toHaveTextContent('(sensitive value)');
    expect(outputs).toHaveTextContent('(known after apply)');
  });

  it('leaves the outputs out when a plan changes none', () => {
    render(<PlanView plan={aRunPlan({ output_changes: [] })} />);

    expect(screen.queryByTestId('output-changes')).not.toBeInTheDocument();
  });

  it('reads a resource with no attributes without failing', async () => {
    render(
      <PlanView
        plan={aRunPlan({
          resource_changes: [
            aResourceChange({
              address: 'null_resource.empty',
              type: 'null_resource',
              name: 'empty',
              action: 'create',
              before: null,
              after: null,
              after_unknown: null,
            }),
          ],
          output_changes: [],
        })}
      />
    );

    const row = await openRow('null_resource.empty');
    expect(row).toHaveTextContent('no attributes to compare');
  });
});
