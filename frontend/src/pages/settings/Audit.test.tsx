import { screen, waitFor } from '@testing-library/react';
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

const { Audit, appendPage, auditQuery, describeChange, formatValue } =
  await import('./Audit');

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
      auditQuery({ action: '', workspaceId: '', days: 1 }, now)
    ).toEqual({ since: '2026-10-08T00:00:00.000Z' });
    expect(
      auditQuery(
        { action: 'variable.written', workspaceId: 'ws-1', days: 7 },
        now
      )
    ).toEqual({
      since: '2026-10-02T00:00:00.000Z',
      action: 'variable.written',
      target_type: 'workspace',
      target_id: 'ws-1',
    });
  });

  it('describes an edit as before and after, anything else by its payload', () => {
    expect(describeChange(anEvent())).toEqual(['description: Old. -> New.']);
    expect(
      describeChange(
        anEvent({
          before: null,
          after: null,
          payload: { key: 'region', category: 'terraform' },
        })
      )
    ).toEqual(['key: region', 'category: terraform']);
    expect(formatValue(null)).toBe('none');
    expect(formatValue(['a', 'b'])).toBe('a, b');
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

  it('lists events and loads the next page', async () => {
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

    expect(
      await screen.findByText('description: Old. -> New.')
    ).toBeInTheDocument();
    expect(screen.getByText('Tyler')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('key: region')).toBeInTheDocument();
    const call = apiMock.listAuditEvents.mock.calls.at(-1)?.[0];
    expect(call?.cursor).toBe('cursor-1');
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Load more' })
      ).not.toBeInTheDocument();
    });
  });

  it('filters by action', async () => {
    apiMock.listAuditEvents.mockResolvedValue({
      items: [anEvent()],
      next_cursor: null,
      event_types: EVENT_TYPES,
    });
    renderPage();
    await screen.findByText('description: Old. -> New.');

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Action' }),
      'variable.written'
    );
    await waitFor(() => {
      const call = apiMock.listAuditEvents.mock.calls.at(-1)?.[0];
      expect(call?.action).toBe('variable.written');
    });
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
    await screen.findByText('description: Old. -> New.');

    await userEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    await waitFor(() => {
      expect(apiMock.exportAuditEvents).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:audit');
    });
  });
});
