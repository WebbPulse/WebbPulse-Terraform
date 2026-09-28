import { describe, expect, it } from 'vitest';

import { isLoopback, loginRequest } from './terraformLoginRequest';

describe('loginRequest', () => {
  it('reads the query Terraform CLI opens the page with', () => {
    const search = new URLSearchParams({
      client_id: 'terraform-cli',
      response_type: 'code',
      redirect_uri: 'http://localhost:10000/login',
      code_challenge: 'c'.repeat(43),
      code_challenge_method: 'S256',
      state: 'abc',
    });
    expect(loginRequest(search)).toEqual({
      client_id: 'terraform-cli',
      response_type: 'code',
      redirect_uri: 'http://localhost:10000/login',
      code_challenge: 'c'.repeat(43),
      code_challenge_method: 'S256',
      state: 'abc',
    });
  });

  it('is null when the page was opened by hand', () => {
    expect(loginRequest(new URLSearchParams())).toBeNull();
  });
});

describe('isLoopback', () => {
  it('accepts only Terraform listening on this machine', () => {
    expect(isLoopback('http://localhost:10003/login?code=x&state=y')).toBe(
      true
    );
    expect(isLoopback('http://127.0.0.1:10000/login')).toBe(true);
    expect(isLoopback('https://evil.example/login?code=x')).toBe(false);
    expect(isLoopback('http://localhost.evil.example:1/login')).toBe(false);
  });
});
