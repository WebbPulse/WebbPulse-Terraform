import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { TerraformApi, describeError } from '../api';
import { jsonResponse, renderWithAuth } from '../test-helpers/renderWithAuth';
import { StepUpBoundary, confirmationWindow } from './StepUpBoundary';

/** The refusal the control plane answers a stale sign-in with. */
function stepUpRequired(): Response {
  return new Response(
    JSON.stringify({
      success: false,
      status: 401,
      message: 'Confirm your password to continue.',
      error_code: 'STEP_UP_REQUIRED',
      max_age: 900,
      request_id: 'r-test',
    }),
    {
      status: 401,
      headers: {
        'content-type': 'application/json',
        'www-authenticate':
          'Bearer error="insufficient_user_authentication", max_age=900',
      },
    }
  );
}

/** A fake backend: the key route refuses until the password is confirmed. */
function backend(correctPassword: string): {
  fetch: typeof globalThis.fetch;
  calls: string[];
} {
  const calls: string[] = [];
  let steppedUp = false;
  const fetch: typeof globalThis.fetch = (input, init) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const path = new URL(url).pathname;
    calls.push(`${init?.method ?? 'GET'} ${path}`);
    if (path.endsWith('/auth/refresh')) {
      return Promise.resolve(
        jsonResponse({ access_token: 'login-token', expires_in: 3600 })
      );
    }
    if (path.endsWith('/auth/step-up')) {
      const raw = typeof init?.body === 'string' ? init.body : '{}';
      const body = JSON.parse(raw) as {
        password?: string;
      };
      if (body.password !== correctPassword) {
        return Promise.resolve(
          jsonResponse(
            {
              success: false,
              status: 401,
              message: 'That password is not right.',
              error_code: 'INVALID_CREDENTIALS',
              request_id: 'r-test',
            },
            401
          )
        );
      }
      steppedUp = true;
      return Promise.resolve(
        jsonResponse({ access_token: 'stepped-token', expires_in: 600 })
      );
    }
    if (path.endsWith('/api-keys')) {
      return Promise.resolve(
        steppedUp
          ? jsonResponse({ key_id: 'k', key: 'wpk_x', name: 'agent' }, 201)
          : stepUpRequired()
      );
    }
    return Promise.resolve(jsonResponse({}, 404));
  };
  return { fetch, calls };
}

/** A button that mints a key and says how it went. */
function MintButton({ api }: { api: TerraformApi }): React.ReactElement {
  const [outcome, setOutcome] = useState('');
  return (
    <>
      <button
        type="button"
        onClick={() => {
          api.createApiKey({ name: 'agent' }).then(
            () => {
              setOutcome('minted');
            },
            (error: unknown) => {
              setOutcome(describeError(error));
            }
          );
        }}
      >
        Mint
      </button>
      <p data-testid="outcome">{outcome}</p>
    </>
  );
}

/** Mounts the boundary around the button, sharing the API's own auth client. */
function mount(password: string): { calls: string[] } {
  const { fetch, calls } = backend(password);
  const api = new TerraformApi({
    baseUrl: 'https://api.test/api/v1',
    fetch,
    retries: 0,
  });
  renderWithAuth(
    <StepUpBoundary api={api}>
      <MintButton api={api} />
    </StepUpBoundary>,
    api.getAuthClient()
  );
  return { calls };
}

describe('confirmationWindow', () => {
  it('says the window in minutes', () => {
    expect(confirmationWindow(900)).toBe('15 minutes');
    expect(confirmationWindow(60)).toBe('1 minute');
    expect(confirmationWindow(null)).toBe('a few minutes');
  });
});

describe('StepUpBoundary', () => {
  it('asks for the password on STEP_UP_REQUIRED and replays the call once', async () => {
    const user = userEvent.setup();
    const { calls } = mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    const dialog = await screen.findByRole('dialog', {
      name: 'Confirm your password',
    });
    expect(dialog).toHaveTextContent('15 minutes');

    await user.type(screen.getByLabelText('Password'), 'hunter22');
    await user.click(screen.getByRole('button', { name: 'Confirm password' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent('minted');
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(2);
  });

  it('keeps the prompt open on a wrong password', async () => {
    const user = userEvent.setup();
    mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await user.type(await screen.findByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Confirm password' }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(
      screen.getByRole('dialog', { name: 'Confirm your password' })
    ).toBeInTheDocument();
    expect(screen.getByTestId('outcome')).toHaveTextContent('');
  });

  it('tells the page nothing changed when the prompt is cancelled', async () => {
    const user = userEvent.setup();
    const { calls } = mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm your password' });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent(
        'Your password was not confirmed, so nothing changed.'
      );
    });
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(1);
  });
});
