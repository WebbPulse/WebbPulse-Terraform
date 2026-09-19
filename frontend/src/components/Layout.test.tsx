import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  renderWithAuth,
  signedInAuthClient,
} from '../test-helpers/renderWithAuth';
import { Layout } from './Layout';

describe('Layout', () => {
  it('renders the rail with both sections and the routed page', () => {
    renderWithAuth(
      <Routes>
        <Route element={<Layout />}>
          <Route path="/runs" element={<p>Runs page</p>} />
        </Route>
      </Routes>,
      signedInAuthClient(),
      ['/runs']
    );
    const rail = screen.getAllByRole('navigation', { name: 'Primary' })[0];
    expect(rail).toBeDefined();
    expect(
      screen.getAllByRole('link', { name: 'Workspaces' })
    ).not.toHaveLength(0);
    expect(screen.getAllByRole('link', { name: 'Runs' })[0]).toHaveAttribute(
      'aria-current',
      'page'
    );
    expect(screen.getByText('Runs page')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Skip to content' })
    ).toHaveAttribute('href', '#main');
  });
});
