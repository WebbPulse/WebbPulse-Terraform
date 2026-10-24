import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import type { AuditEvent } from '../../api';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';
import { aListedWorkspace, aWorkspace } from '../../test-helpers/fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());

const {
  Audit,
  actorLabel,
  appendPage,
  auditQuery,
  changedFields,
  describeChange,
  formatDetail,
  formatValue,
  mergeActors,
  verbPhrase,
} = await import('./Audit');

/** A recorded event, a workspace edit unless overridden. */
function anEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: '01J0000000000000000000000A',
    action: 'workspace.updated',
    label: 'Workspace updated',
    occurred_at: new Date().toISOString(),
    actor_id: 'user-1',
    actor_kind: 'user',
    actor_name: 'Tyler',
    source: 'web',
    ip: '203.0.113.7',
    amr: ['pwd'],
    target_type: 'workspace',
    target_id: 'ws-1',
    target_label: 'platform',
    payload: {},
    before: { description: 'Old.' },
    after: { description: 'New.' },
    ...overrides,
  };
}

/** The event types the listing offers. */
const EVENT_TYPES = [
  { action: 'workspace.updated', label: 'Workspace updated' },
  { action: 'variable.written', label: 'Variable written' },
];

/** Mounts the page at its route. */
function renderPage(): void {
  renderWithAuth(
    <Routes>
      <Route path="/settings/audit" element={<Audit />} />
    </Routes>,
    signedInAuthClient(),
    ['/settings/audit']
  );
}

describe('Audit helpers', () => {
  it('builds the query from the filters', () => {
    const now = Date.parse('2026-10-09T00:00:00Z');
    expect(
      auditQuery({ action: '', actorId: '', workspaceId: '', days: 1 }, now)
    ).toEqual({
      since: '2026-10-08T00:00:00.000Z',
    });
    expect(
      auditQuery(
        {
          action: 'variable.written',
          actorId: 'user-2',
          workspaceId: 'ws-1',
          days: 7,
        },
        now
      )
    ).toEqual({
      since: '2026-10-02T00:00:00.000Z',
      action: 'variable.written',
      actor_id: 'user-2',
      target_type: 'workspace',
      target_id: 'ws-1',
    });
  });

  it('keeps only the fields an edit changed, the payload otherwise', () => {
    const edit = anEvent({
      before: { description: 'Old.', name: 'platform', tags: ['a'] },
      after: { description: 'New.', name: 'platform', tags: ['a'] },
    });
    expect(changedFields(edit).map(describeChange)).toEqual([
      'description: Old. -> New.',
    ]);
    expect(
      changedFields(
        anEvent({
          before: null,
          after: null,
          payload: { key: 'region', category: 'terraform' },
        })
      ).map(describeChange)
    ).toEqual(['key: region', 'category: terraform']);
    expect(
      changedFields(anEvent({ before: { a: 1 }, after: { a: 1 } }))
    ).toEqual([]);
    expect(formatValue(null)).toBe('none');
    expect(formatValue(['a', 'b'])).toBe('a, b');
  });

  it('prints objects in full for the detail view', () => {
    expect(formatDetail({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(formatDetail(['a', 'b'])).toBe('a, b');
    expect(formatDetail('text')).toBe('text');
  });

  it('names actors and phrases actions mid sentence', () => {
    expect(actorLabel(anEvent())).toBe('Tyler');
    expect(
      actorLabel(anEvent({ actor_kind: 'run_token', actor_id: 'run-1' }))
    ).toBe('A run');
    expect(verbPhrase('Workspace updated')).toBe('workspace updated');
    expect(verbPhrase('API key created')).toBe('API key created');
  });

  it('offers each person once, runs left out, held ones kept', () => {
    const held = [{ id: 'user-0', label: 'Alex' }];
    const actors = mergeActors(held, [
      anEvent(),
      anEvent({ event_id: '2' }),
      anEvent({ event_id: '3', actor_kind: 'run_token', actor_id: 'run-1' }),
    ]);
    expect(actors).toEqual([
      { id: 'user-0', label: 'Alex' },
      { id: 'user-1', label: 'Tyler' },
    ]);
  });

  it('appends a page without repeating an event', () => {
    const one = anEvent({ event_id: '1' });
    const two = anEvent({ event_id: '2' });
    expect(appendPage([one], [one, two])).toEqual([one, two]);
  });
});

describe('Audit', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.listWorkspaces.mockResolvedValue({
      items: [aListedWorkspace(aWorkspace({ workspace_id: 'ws-1' }))],
    });
  });

  it('says when nothing was recorded', async () => {
    apiMock.listAuditEvents.mockResolvedValue({
      items: [],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    renderPage();
    expect(
      await screen.findByText('No events in this range')
    ).toBeInTheDocument();
  });

  it('lists events as one line each and loads the next page', async () => {
    apiMock.listAuditEvents
      .mockResolvedValueOnce({
        items: [anEvent()],
        next_cursor: 'cursor-1',
        event_types: EVENT_TYPES,
      })
      .mockResolvedValue({
        items: [
          anEvent({
            event_id: '01J0000000000000000000000B',
            action: 'variable.written',
            label: 'Variable written',
            before: null,
            after: null,
            payload: { key: 'region', category: 'env' },
          }),
        ],
        next_cursor: null,
        event_types: EVENT_TYPES,
      });
    renderPage();

    const row = await screen.findByRole('button', {
      name: /Tyler workspace updated platform/,
    });
    expect(row).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('203.0.113.7')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(
      await screen.findByRole('button', {
        name: /Tyler variable written platform/,
      })
    ).toBeInTheDocument();
    const call = apiMock.listAuditEvents.mock.calls.at(-1)?.[0];
    expect(call?.cursor).toBe('cursor-1');
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Load more' })
      ).not.toBeInTheDocument();
    });
  });

  it('opens an event to its changed fields, IP and ids', async () => {
    const arn =
      'arn:aws:iam::123456789012:role/webbpulse-terraform-prod-workspace/a-very-long-role-name';
    apiMock.listAuditEvents.mockResolvedValue({
      items: [
        anEvent({
          before: { description: 'Old.', run_role_arn: null, name: 'platform' },
          after: { description: 'New.', run_role_arn: arn, name: 'platform' },
        }),
      ],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    renderPage();

    const row = await screen.findByRole('button', {
      name: /Tyler workspace updated platform/,
    });
    await userEvent.click(row);
    expect(row).toHaveAttribute('aria-expanded', 'true');

    const changes = screen.getByRole('region', { name: 'Changes' });
    expect(within(changes).getByText('description')).toBeInTheDocument();
    expect(within(changes).getByText('run_role_arn')).toBeInTheDocument();
    expect(within(changes).queryByText('name')).not.toBeInTheDocument();
    const value = within(changes).getByText(arn);
    expect(value).toHaveClass('break-words');
    expect(value).not.toHaveClass('break-all');
    expect(screen.getByText('203.0.113.7')).toBeInTheDocument();
    expect(screen.getByText('ws-1')).toBeInTheDocument();
    expect(screen.getByText('01J0000000000000000000000A')).toBeInTheDocument();

    await userEvent.click(row);
    expect(screen.queryByText('203.0.113.7')).not.toBeInTheDocument();
  });

  it('filters by action', async () => {
    apiMock.listAuditEvents.mockResolvedValue({
      items: [anEvent()],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    renderPage();
    await screen.findByRole('button', { name: /Tyler workspace updated/ });

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Action' }),
      'variable.written'
    );
    await waitFor(() => {
      const call = apiMock.listAuditEvents.mock.calls.at(-1)?.[0];
      expect(call?.action).toBe('variable.written');
    });
  });

  it('filters by actor and keeps the other actors on offer', async () => {
    apiMock.listAuditEvents.mockResolvedValue({
      items: [
        anEvent(),
        anEvent({
          event_id: '01J0000000000000000000000C',
          actor_id: 'user-2',
          actor_name: 'Robin',
        }),
        anEvent({
          event_id: '01J0000000000000000000000D',
          actor_kind: 'run_token',
          actor_id: 'run-1',
          actor_name: null,
        }),
      ],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    renderPage();
    await screen.findByRole('button', { name: /Robin workspace updated/ });

    const actor = screen.getByRole('combobox', { name: 'Actor' });
    expect(
      within(actor)
        .getAllByRole('option')
        .map((option) => option.textContent)
    ).toEqual(['All actors', 'Robin', 'Tyler']);

    apiMock.listAuditEvents.mockResolvedValue({
      items: [
        anEvent({
          event_id: '01J0000000000000000000000C',
          actor_id: 'user-2',
          actor_name: 'Robin',
        }),
      ],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    await userEvent.selectOptions(actor, 'user-2');
    await waitFor(() => {
      const call = apiMock.listAuditEvents.mock.calls.at(-1)?.[0];
      expect(call?.actor_id).toBe('user-2');
    });
    expect(
      within(screen.getByRole('combobox', { name: 'Actor' })).getByRole(
        'option',
        { name: 'Tyler' }
      )
    ).toBeInTheDocument();
  });

  it('exports the filtered events as CSV', async () => {
    apiMock.listAuditEvents.mockResolvedValue({
      items: [anEvent()],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    apiMock.exportAuditEvents.mockResolvedValue('occurred_at,action\n');
    const createObjectURL = vi.fn(() => 'blob:audit');
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    renderPage();
    await screen.findByRole('button', { name: /Tyler workspace updated/ });

    await userEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    await waitFor(() => {
      expect(apiMock.exportAuditEvents).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:audit');
    });
  });
});
