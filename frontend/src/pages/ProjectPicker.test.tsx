import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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

const { ProjectPicker } = await import('./ProjectPicker');

describe('ProjectPicker', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('still offers the default project while it has no workspaces, since it is the fallback destination', async () => {
    apiMock.listProjects.mockResolvedValue({
      items: [
        {
          project_id: 'prj-default',
          name: 'Default Project',
          is_default: true,
          workspace_count: 0,
        },
        {
          project_id: 'prj-01J000000000000000000001',
          name: 'CarModPicker',
          is_default: false,
          workspace_count: 2,
        },
      ],
    });
    renderWithAuth(
      <ProjectPicker value="prj-default" onChange={vi.fn()} />,
      signedInAuthClient()
    );

    const select = await screen.findByLabelText('Project');
    const options = within(select)
      .getAllByRole('option')
      .map((option) => option.textContent);
    expect(options).toEqual(['Default Project', 'CarModPicker']);
  });
});
