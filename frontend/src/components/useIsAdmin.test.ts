import { describe, expect, it } from 'vitest';

import { rolesFromToken } from './useIsAdmin';

/** A JWT shaped string carrying `claims`, unsigned since only the payload is read. */
function tokenWith(claims: object): string {
  const payload = btoa(JSON.stringify(claims))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  return `e30.${payload}.sig`;
}

describe('rolesFromToken', () => {
  it('reads the roles list', () => {
    expect(rolesFromToken(tokenWith({ roles: ['admin'] }))).toEqual(['admin']);
  });

  it('reads a single role string', () => {
    expect(rolesFromToken(tokenWith({ roles: 'admin' }))).toEqual(['admin']);
  });

  it('is empty for no token, an opaque token or no roles', () => {
    expect(rolesFromToken(null)).toEqual([]);
    expect(rolesFromToken('test-token')).toEqual([]);
    expect(rolesFromToken('a.!!!.c')).toEqual([]);
    expect(rolesFromToken(tokenWith({ sub: 'u1' }))).toEqual([]);
  });
});
