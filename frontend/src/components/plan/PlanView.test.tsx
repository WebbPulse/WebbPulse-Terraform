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

/** Switches the resource list to every resource. */
async function showAll(): Promise<void> {
  await userEvent.click(screen.getByRole('tab', { name: /^All/ }));
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

    expect(screen.getAllByTestId('resource-change')).toHaveLength(5);
    expect(
      screen.queryByRole('listitem', { name: /aws_kms_key/ })
    ).not.toBeInTheDocument();
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

  it('leaves an unchanged resource with nothing to open', async () => {
    render(<PlanView plan={aRunPlan()} />);
    await showAll();

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

  it('shows the apply summary once the run applied', () => {
    render(
      <PlanView
        plan={aRunPlan()}
        applyChanges={{ add: 1, change: 0, destroy: 0 }}
      />
    );

    expect(screen.getByTestId('apply-summary-line')).toHaveTextContent(
      'Apply complete! Resources: 1 added, 0 changed, 0 destroyed.'
    );
  });

  it('names a destroy run in the apply summary', () => {
    render(
      <PlanView
        plan={aRunPlan()}
        applyChanges={{ add: 0, change: 0, destroy: 2 }}
        isDestroy
      />
    );

    expect(screen.getByTestId('apply-summary-line')).toHaveTextContent(
      'Destroy complete! Resources: 0 added, 0 changed, 2 destroyed.'
    );
  });

  it('shows no apply summary before an apply', () => {
    render(<PlanView plan={aRunPlan()} applyChanges={null} />);

    expect(screen.queryByTestId('apply-summary-line')).not.toBeInTheDocument();
  });

  it('fills applied output values and keeps sensitive ones hidden', () => {
    render(
      <PlanView
        plan={aRunPlan({
          applied_outputs: [
            { name: 'endpoint', value: 'db.example.com', sensitive: false },
            {
              name: 'database_password',
              value: '(sensitive value)',
              sensitive: true,
            },
          ],
        })}
      />
    );

    const outputs = screen.getByTestId('output-changes');
    expect(outputs).toHaveTextContent('"db.example.com"');
    expect(outputs).not.toHaveTextContent('(known after apply)');
    const password = outputs.querySelector('[data-output="database_password"]');
    expect(password).toHaveTextContent('(sensitive value)');
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

  describe('resource filter', () => {
    it('opens on the changed resources and counts both views', () => {
      render(<PlanView plan={aRunPlan()} />);

      const changed = screen.getByRole('tab', { name: 'Changed (5)' });
      expect(changed).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tab', { name: 'All (6)' })).toHaveAttribute(
        'aria-selected',
        'false'
      );
      expect(screen.getByTestId('resource-filter-caption')).toHaveTextContent(
        '1 unchanged resource hidden'
      );
      expect(
        screen
          .getAllByTestId('resource-change')
          .every((row) => row.dataset['changed'] === 'true')
      ).toBe(true);
    });

    it('shows every resource, unchanged ones included, on demand', async () => {
      render(<PlanView plan={aRunPlan()} />);
      await showAll();

      expect(screen.getByTestId('resource-section')).toHaveAttribute(
        'data-filter',
        'all'
      );
      expect(screen.getAllByTestId('resource-change')).toHaveLength(6);
      expect(rowFor('aws_kms_key.state')).toHaveAttribute(
        'data-action',
        'no-op'
      );
      expect(screen.getByTestId('resource-filter-caption')).toHaveTextContent(
        'Showing all 6 resources'
      );

      await userEvent.click(screen.getByRole('tab', { name: /^Changed/ }));
      expect(screen.getAllByTestId('resource-change')).toHaveLength(5);
    });

    it('says so plainly when nothing changes, with every resource a click away', async () => {
      render(
        <PlanView
          plan={aRunPlan({
            changes: { add: 0, change: 0, destroy: 0 },
            resource_changes: [
              aResourceChange({ address: 'aws_s3_bucket.a', action: 'no-op' }),
              aResourceChange({ address: 'aws_s3_bucket.b', action: 'no-op' }),
            ],
            output_changes: [],
            has_changes: false,
          })}
        />
      );

      expect(screen.getByTestId('plan-summary-line')).toHaveTextContent(
        'No changes.'
      );
      expect(
        screen.getByText('No resources change in this plan.')
      ).toBeInTheDocument();
      expect(screen.queryByTestId('resource-changes')).not.toBeInTheDocument();

      await userEvent.click(
        screen.getByRole('button', { name: 'Show all 2 resources' })
      );
      expect(screen.getAllByTestId('resource-change')).toHaveLength(2);
      expect(screen.getByRole('tab', { name: 'All (2)' })).toHaveAttribute(
        'aria-selected',
        'true'
      );
    });

    it('renders no resource section for a plan with no resources at all', () => {
      render(<PlanView plan={anEmptyRunPlan()} />);

      expect(screen.queryByTestId('resource-section')).not.toBeInTheDocument();
    });

    it('keeps imports and moves in the changed view and labels them', () => {
      render(
        <PlanView
          plan={aRunPlan({
            changes: { add: 0, change: 0, destroy: 0 },
            resource_changes: [
              aResourceChange({
                address: 'aws_s3_bucket.imported',
                action: 'no-op',
                importing: true,
              }),
              aResourceChange({
                address: 'aws_s3_bucket.renamed',
                action: 'no-op',
                previous_address: 'aws_s3_bucket.old',
              }),
              aResourceChange({
                address: 'aws_s3_bucket.same',
                action: 'no-op',
              }),
            ],
            output_changes: [],
            has_changes: false,
          })}
        />
      );

      expect(
        screen.getByRole('tab', { name: 'Changed (2)' })
      ).toBeInTheDocument();
      expect(
        within(rowFor('aws_s3_bucket.imported')).getByTestId('import-badge')
      ).toHaveTextContent('import');
      expect(
        within(rowFor('aws_s3_bucket.renamed')).getByTestId('moved-from')
      ).toHaveTextContent('moved from aws_s3_bucket.old');
      expect(
        within(rowFor('aws_s3_bucket.imported')).getByRole('button')
      ).toBeEnabled();
      expect(screen.getByTestId('plan-import-count')).toHaveTextContent(
        '1 to import'
      );
      expect(screen.getByTestId('plan-move-count')).toHaveTextContent(
        '1 to move'
      );
    });

    it('counts a forget and keeps it in the changed view', () => {
      render(
        <PlanView
          plan={aRunPlan({
            changes: { add: 0, change: 0, destroy: 0 },
            resource_changes: [
              aResourceChange({
                address: 'aws_s3_bucket.kept',
                action: 'forget',
              }),
            ],
            output_changes: [],
            has_changes: false,
          })}
        />
      );

      expect(rowFor('aws_s3_bucket.kept')).toHaveAttribute(
        'data-action',
        'forget'
      );
      expect(screen.getByTestId('plan-forget-count')).toHaveTextContent(
        '1 to forget'
      );
    });

    it('says how many unchanged resources a large plan left out', async () => {
      render(<PlanView plan={aRunPlan({ unchanged_omitted: 812 })} />);

      expect(
        screen.getByRole('tab', { name: 'All (818)' })
      ).toBeInTheDocument();
      expect(screen.queryByTestId('unchanged-omitted')).not.toBeInTheDocument();

      await showAll();
      expect(screen.getByTestId('unchanged-omitted')).toHaveTextContent(
        '812 unchanged resources not listed'
      );
    });

    it('keeps the same default under an applied plan', () => {
      render(
        <PlanView
          plan={aRunPlan()}
          applyChanges={{ add: 2, change: 1, destroy: 1 }}
        />
      );

      expect(screen.getByRole('tab', { name: 'Changed (5)' })).toHaveAttribute(
        'aria-selected',
        'true'
      );
      expect(screen.getAllByTestId('resource-change')).toHaveLength(5);
    });
  });
});
