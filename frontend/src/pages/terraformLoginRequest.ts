/** Reading and checking the request `terraform login` opens the approve page with. */

import type { LoginAuthorizationCreate } from '../api';

const LOOPBACK = /^http:\/\/(localhost|127\.0\.0\.1):\d+\/login(\?|$)/;

/** The request Terraform opened the page with, or null when a field is missing. */
export function loginRequest(
  search: URLSearchParams
): LoginAuthorizationCreate | null {
  const field = (name: string): string => search.get(name) ?? '';
  const request: LoginAuthorizationCreate = {
    client_id: field('client_id'),
    response_type: field('response_type'),
    redirect_uri: field('redirect_uri'),
    code_challenge: field('code_challenge'),
    code_challenge_method: field('code_challenge_method'),
    state: field('state'),
  };
  return request.client_id === '' ||
    request.redirect_uri === '' ||
    request.code_challenge === ''
    ? null
    : request;
}

/** Whether a redirect is Terraform's loopback listener, the only place a code may go. */
export function isLoopback(url: string): boolean {
  return LOOPBACK.test(url);
}
