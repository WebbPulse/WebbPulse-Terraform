import { screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aRun } from '../test-helpers/fixtures';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../test-helpers/apiMock';

vi.mock('../api/client', () => apiClientModuleMock());

const { Runs } = await import('./Runs');

describe('Runs', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('lists every run with its state and plan counts', async () => {
    apiMock.listRuns.mockResolvedValue({
      items: [
        aRun('applied'),
        aRun('errored', { run_id: 'run-2', changes: null }),
      ],
    });

    renderWithAuth(<Runs />, signedInAuthClient());

    const badges = await screen.findAllByTestId('run-state-badge');
    expect(badges[0]).toHaveAttribute('data-state', 'applied');
    expect(badges[1]).toHaveAttribute('data-state', 'errored');
    expect(screen.getByText('+3 ~1 -0')).toBeInTheDocument();
  });

  it('says so when there are no runs', async () => {
    apiMock.listRuns.mockResolvedValue({ items: [] });

    renderWithAuth(<Runs />, signedInAuthClient());

    expect(await screen.findByText('No runs yet.')).toBeInTheDocument();
  });
});
