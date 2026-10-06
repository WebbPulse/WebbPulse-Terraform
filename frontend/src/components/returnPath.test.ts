import { describe, expect, it } from 'vitest';

import { deviceHandOff } from './returnPath';

const IDENTITY = 'https://api.terraform.example';
const PAGE = 'https://terraform.example';
const APPROVAL = `${IDENTITY}/api/auth/device?user_code=ABCD-EFGH`;

/** The sign-in query a hand-off arrives with. */
function search(returnTo: string, prompt?: string): string {
  const params = new URLSearchParams({ returnTo });
  if (prompt !== undefined) {
    params.set('prompt', prompt);
  }
  return `?${params.toString()}`;
}

describe('deviceHandOff', () => {
  it('accepts the approval page on the identity origin with its query', () => {
    expect(deviceHandOff(search(APPROVAL), IDENTITY, PAGE)).toEqual({
      returnTo: APPROVAL,
      reauthenticate: false,
    });
  });

  it('reads prompt=login as a fresh sign-in', () => {
    expect(
      deviceHandOff(search(APPROVAL, 'login'), IDENTITY, PAGE)?.reauthenticate
    ).toBe(true);
  });

  it('is null without a returnTo', () => {
    expect(deviceHandOff('?prompt=login', IDENTITY, PAGE)).toBeNull();
  });

  it.each([
    ['another origin', 'https://evil.example/api/auth/device'],
    ['another path', `${IDENTITY}/api/auth/device/approve`],
    ['a prefix path', `${IDENTITY}/api/auth/devices`],
    ['plain http', 'http://api.terraform.example/api/auth/device'],
    ['credentials', 'https://user:pass@api.terraform.example/api/auth/device'],
    ['a fragment', `${IDENTITY}/api/auth/device#x`],
    ['a relative path', '/api/auth/device'],
    ['a scheme relative URL', '//evil.example/api/auth/device'],
    ['a script URL', 'javascript:alert(1)'],
  ])('refuses %s', (_label, returnTo) => {
    expect(deviceHandOff(search(returnTo), IDENTITY, PAGE)).toBeNull();
  });

  it('falls back to the page origin when the identity origin is relative', () => {
    const approval = `${PAGE}/api/auth/device`;
    expect(deviceHandOff(search(approval), '', PAGE)?.returnTo).toBe(approval);
  });
});
