import { ApiError } from '@webbpulse/api-client';
import { describe, expect, it } from 'vitest';

import { aDelivery, aNotification } from '../../../test-helpers/fixtures';
import {
  createBody,
  deliverySummary,
  draftFrom,
  draftProblem,
  testSendError,
  updateBody,
} from './notifications';

describe('createBody', () => {
  it('orders the triggers as a run moves and trims the text fields', () => {
    expect(
      createBody({
        ...draftFrom(null),
        name: ' Alerts ',
        url: ' https://hooks.slack.com/services/T/B/x ',
        triggers: ['run:errored', 'run:created'],
      })
    ).toEqual({
      name: 'Alerts',
      destination_type: 'slack',
      url: 'https://hooks.slack.com/services/T/B/x',
      triggers: ['run:created', 'run:errored'],
      enabled: true,
    });
  });

  it('drops a token typed before switching away from a generic webhook', () => {
    const body = createBody({
      ...draftFrom(null),
      name: 'Alerts',
      url: 'https://hooks.slack.com/services/T/B/x',
      token: 'left over',
    });
    expect(body).not.toHaveProperty('token');
  });
});

describe('updateBody', () => {
  it('leaves the URL and token out when both are blank', () => {
    const editing = aNotification({
      destination_type: 'generic',
      has_token: true,
    });
    const body = updateBody(editing, draftFrom(editing));
    expect(body).not.toHaveProperty('url');
    expect(body).not.toHaveProperty('token');
    expect(body).not.toHaveProperty('destination_type');
  });

  it('clears the token when the destination moves off a generic webhook', () => {
    const editing = aNotification({
      destination_type: 'generic',
      has_token: true,
    });
    expect(
      updateBody(editing, {
        ...draftFrom(editing),
        destination: 'slack',
        url: 'https://hooks.slack.com/services/T/B/x',
      })
    ).toEqual(
      expect.objectContaining({
        destination_type: 'slack',
        url: 'https://hooks.slack.com/services/T/B/x',
        token: '',
      })
    );
  });

  it('sends a new token in place of the stored one', () => {
    const editing = aNotification({
      destination_type: 'generic',
      has_token: true,
    });
    expect(
      updateBody(editing, { ...draftFrom(editing), token: 'rotated' }).token
    ).toBe('rotated');
  });
});

describe('draftProblem', () => {
  it('needs a new URL when the destination changes', () => {
    const editing = aNotification();
    expect(
      draftProblem({ ...draftFrom(editing), destination: 'discord' }, editing)
    ).toBe('Changing the destination needs a new URL.');
  });

  it('lets an edit keep its URL', () => {
    const editing = aNotification();
    expect(draftProblem(draftFrom(editing), editing)).toBeNull();
  });
});

describe('deliverySummary', () => {
  it('names the outcome and the status code', () => {
    expect(deliverySummary(aDelivery())).toBe('Delivered, HTTP 200');
    expect(
      deliverySummary(aDelivery({ status: 'retrying', status_code: null }))
    ).toBe('Retrying');
  });
});

describe('testSendError', () => {
  it('rounds the Retry-After wait up to whole seconds', () => {
    const error = new ApiError({
      status: 429,
      statusText: 'Too Many Requests',
      url: 'https://api.test/verify',
      method: 'POST',
      body: null,
      retryAfterSeconds: 2.4,
    });
    expect(testSendError(error)).toBe(
      'Too many test sends for this notification. Try again in 3 seconds.'
    );
  });
});
