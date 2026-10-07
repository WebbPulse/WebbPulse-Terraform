/** The rules behind the notifications page: labels, the form draft and the request bodies. */

import { ApiError } from '@webbpulse/api-client';

import {
  describeError,
  type NotificationConfiguration,
  type NotificationConfigurationCreate,
  type NotificationConfigurationUpdate,
  type NotificationDelivery,
  type NotificationDestination,
  type NotificationTrigger,
} from '../../../api';

/** The most notification configurations one workspace holds. */
export const MAX_NOTIFICATIONS = 50;

/** The run events a configuration can fire on, in run order, with their labels. */
export const TRIGGERS: readonly {
  id: NotificationTrigger;
  label: string;
  hint: string;
}[] = [
  { id: 'run:created', label: 'Created', hint: 'A run is queued.' },
  { id: 'run:planning', label: 'Planning', hint: 'A plan starts.' },
  {
    id: 'run:needs_attention',
    label: 'Needs attention',
    hint: 'A plan waits for someone to confirm or discard it.',
  },
  { id: 'run:applying', label: 'Applying', hint: 'An apply starts.' },
  {
    id: 'run:completed',
    label: 'Completed',
    hint: 'A run finishes, applied or with nothing to apply.',
  },
  { id: 'run:errored', label: 'Errored', hint: 'A plan or apply fails.' },
];

/** The destinations a configuration can deliver to, with their labels. */
export const DESTINATIONS: readonly {
  id: NotificationDestination;
  label: string;
}[] = [
  { id: 'slack', label: 'Slack' },
  { id: 'discord', label: 'Discord' },
  { id: 'generic', label: 'Webhook' },
];

/** The label of one trigger. */
export function triggerLabel(trigger: string): string {
  return TRIGGERS.find((item) => item.id === trigger)?.label ?? trigger;
}

/** The label of one destination. */
export function destinationLabel(destination: NotificationDestination): string {
  return (
    DESTINATIONS.find((item) => item.id === destination)?.label ?? destination
  );
}

/** The hint under the URL field for each destination. */
export function urlHint(destination: NotificationDestination): string {
  if (destination === 'slack') {
    return 'A Slack incoming webhook URL, starting https://hooks.slack.com/.';
  }
  if (destination === 'discord') {
    return 'A Discord channel webhook URL, starting https://discord.com/api/webhooks/.';
  }
  return 'Any public HTTPS URL. It receives the HCP Terraform notification payload.';
}

/** What the form holds while someone fills it in. */
export interface NotificationDraft {
  name: string;
  destination: NotificationDestination;
  url: string;
  token: string;
  /** Removes the stored token on save. Only meaningful when editing a generic webhook. */
  clearToken: boolean;
  triggers: NotificationTrigger[];
  enabled: boolean;
}

/** The draft a new form starts from, or one seeded from the configuration being edited. */
export function draftFrom(
  editing: NotificationConfiguration | null
): NotificationDraft {
  return {
    name: editing?.name ?? '',
    destination: editing?.destination_type ?? 'slack',
    url: '',
    token: '',
    clearToken: false,
    triggers: editing?.triggers ?? [],
    enabled: editing?.enabled ?? true,
  };
}

/** The triggers in run order, so the request and the list read the same way. */
function ordered(
  triggers: readonly NotificationTrigger[]
): NotificationTrigger[] {
  return TRIGGERS.map((item) => item.id).filter((id) => triggers.includes(id));
}

/** Why the draft cannot be saved yet, or null when it can. */
export function draftProblem(
  draft: NotificationDraft,
  editing: NotificationConfiguration | null
): string | null {
  if (draft.name.trim() === '') {
    return 'Enter a name.';
  }
  if (editing === null && draft.url.trim() === '') {
    return 'Enter the webhook URL.';
  }
  if (
    editing !== null &&
    editing.destination_type !== draft.destination &&
    draft.url.trim() === ''
  ) {
    return 'Changing the destination needs a new URL.';
  }
  return null;
}

/** The body that creates a configuration from the draft. */
export function createBody(
  draft: NotificationDraft
): NotificationConfigurationCreate {
  const body: NotificationConfigurationCreate = {
    name: draft.name.trim(),
    destination_type: draft.destination,
    url: draft.url.trim(),
    triggers: ordered(draft.triggers),
    enabled: draft.enabled,
  };
  if (draft.destination === 'generic' && draft.token !== '') {
    body.token = draft.token;
  }
  return body;
}

/**
 * The edit the draft makes to a stored configuration.
 *
 * A blank URL keeps the stored one. A blank token keeps the stored one too, and
 * an empty string is sent only to clear it: when asked, or when the destination
 * moves off a generic webhook, since only a generic webhook takes a token.
 */
export function updateBody(
  editing: NotificationConfiguration,
  draft: NotificationDraft
): NotificationConfigurationUpdate {
  const body: NotificationConfigurationUpdate = {
    name: draft.name.trim(),
    triggers: ordered(draft.triggers),
    enabled: draft.enabled,
  };
  if (draft.destination !== editing.destination_type) {
    body.destination_type = draft.destination;
  }
  if (draft.url.trim() !== '') {
    body.url = draft.url.trim();
  }
  if (draft.destination === 'generic') {
    if (draft.token !== '') {
      body.token = draft.token;
    } else if (draft.clearToken && editing.has_token) {
      body.token = '';
    }
  } else if (editing.has_token) {
    body.token = '';
  }
  return body;
}

/** The one line describing a delivery's outcome, such as "Delivered, HTTP 200". */
export function deliverySummary(delivery: NotificationDelivery): string {
  const code =
    delivery.status_code === null || delivery.status_code === undefined
      ? ''
      : `, HTTP ${String(delivery.status_code)}`;
  if (delivery.status === 'succeeded') {
    return `Delivered${code}`;
  }
  if (delivery.status === 'retrying') {
    return `Retrying${code}`;
  }
  return `Failed${code}`;
}

/** The sentence for a test send that did not go out, with the wait a 429 asks for. */
export function testSendError(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    const seconds = error.retryAfterSeconds;
    return seconds === undefined
      ? 'Too many test sends for this notification. Try again in a minute.'
      : `Too many test sends for this notification. Try again in ${String(Math.max(1, Math.ceil(seconds)))} seconds.`;
  }
  return describeError(error);
}
