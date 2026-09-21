/** Turning a plan's before and after objects into the rows a diff renders. */

import type { PlanAction, PlanResourceChange } from '../../api/runPlan';

/** The glyph Terraform prints beside a resource for each action. */
const GLYPHS: Record<PlanAction, string> = {
  create: '+',
  update: '~',
  delete: '-',
  replace: '-/+',
  read: '<=',
  'no-op': ' ',
};

/** The glyph for an action, as the engine's own plan output prints it. */
export function actionGlyph(action: PlanAction): string {
  return GLYPHS[action];
}

/** The words for an action, for a screen reader and the row's label. */
const ACTION_LABELS: Record<PlanAction, string> = {
  create: 'created',
  update: 'updated in place',
  delete: 'destroyed',
  replace: 'replaced',
  read: 'read',
  'no-op': 'unchanged',
};

/** What the action does to the resource, in words. */
export function actionLabel(action: PlanAction): string {
  return ACTION_LABELS[action];
}

/** The token family a row is coloured from. */
export type ActionTone =
  'add' | 'change' | 'destroy' | 'replace' | 'read' | 'none';

/** The colour family for an action. */
export function actionTone(action: PlanAction): ActionTone {
  switch (action) {
    case 'create':
      return 'add';
    case 'update':
      return 'change';
    case 'delete':
      return 'destroy';
    case 'replace':
      return 'replace';
    case 'read':
      return 'read';
    case 'no-op':
      return 'none';
  }
}

/** What happened to one attribute between before and after. */
export type AttributeKind = 'added' | 'removed' | 'changed' | 'unchanged';

/** One attribute of a resource, as the diff shows it. */
export interface AttributeDiff {
  /** The attribute's name. */
  key: string;
  kind: AttributeKind;
  /** The value before, or undefined when the attribute is new. */
  before: unknown;
  /** The value after, or undefined when the attribute is going away. */
  after: unknown;
  /** Whether the value after is only known once the change is applied. */
  afterUnknown: boolean;
  /** Whether either side is sensitive, so no value is printed. */
  sensitive: boolean;
  /** Whether changing this attribute is what forces the resource to be replaced. */
  forcesReplacement: boolean;
}

/** A record with unknown values, which is what a plan's objects are. */
type Bag = Record<string, unknown>;

/** Reads a value as a bag, or an empty one when it is not an object. */
function asBag(value: unknown): Bag {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Bag)
    : {};
}

/** Whether a sensitive marker covers this key. */
function isSensitiveAt(marker: unknown, key: string): boolean {
  if (marker === true) {
    return true;
  }
  const bag = asBag(marker);
  return bag[key] === true || (key in bag && bag[key] !== false);
}

/**
 * Whether the value after is unknown at plan time.
 *
 * `after_unknown` mirrors the shape of the resource, carrying `true` at a key
 * whose value the engine cannot know until the change is applied.
 */
function isUnknownAt(afterUnknown: unknown, key: string): boolean {
  if (afterUnknown === true) {
    return true;
  }
  const bag = asBag(afterUnknown);
  const at = bag[key];
  if (at === true) {
    return true;
  }
  if (Array.isArray(at)) {
    return at.some((entry) => entry === true);
  }
  if (typeof at === 'object' && at !== null) {
    return Object.values(at).some((entry) => entry === true);
  }
  return false;
}

/** Whether two plan values are the same, compared structurally. */
function sameValue(left: unknown, right: unknown): boolean {
  if (left === right) {
    return true;
  }
  if (typeof left !== typeof right) {
    return false;
  }
  if (typeof left !== 'object' || left === null || right === null) {
    return false;
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

/** The attribute names a replace path points at, at the top level. */
function replacedKeys(paths: (string | number)[][]): Set<string> {
  const keys = new Set<string>();
  for (const path of paths) {
    const head = path[0];
    if (typeof head === 'string') {
      keys.add(head);
    }
  }
  return keys;
}

/**
 * Every attribute of a resource change, ordered by name.
 *
 * Keys are taken from before, after and `after_unknown` together, so an
 * attribute that only exists on one side is still listed. A `delete` is read
 * as removing every attribute, and a `create` as adding every one, which is
 * how the engine's own output reads them.
 */
export function attributeDiffs(
  change: PlanResourceChange
): readonly AttributeDiff[] {
  const before = asBag(change.before);
  const after = asBag(change.after);
  const unknown = asBag(change.after_unknown);
  const forced = replacedKeys(change.replace_paths ?? []);
  const keys = [
    ...new Set([
      ...Object.keys(before),
      ...Object.keys(after),
      ...Object.keys(unknown),
    ]),
  ].sort((left, right) => left.localeCompare(right));

  return keys.map((key) => {
    const hasBefore = key in before;
    const hasAfter = key in after;
    const afterUnknown = isUnknownAt(change.after_unknown, key);
    const sensitive =
      isSensitiveAt(change.before_sensitive, key) ||
      isSensitiveAt(change.after_sensitive, key);
    const beforeValue = before[key];
    const afterValue = after[key];

    let kind: AttributeKind;
    if (afterUnknown && !hasAfter) {
      kind = hasBefore ? 'changed' : 'added';
    } else if (!hasBefore && hasAfter) {
      kind = 'added';
    } else if (hasBefore && !hasAfter) {
      kind = 'removed';
    } else if (sameValue(beforeValue, afterValue)) {
      kind = 'unchanged';
    } else {
      kind = 'changed';
    }

    return {
      key,
      kind,
      before: beforeValue,
      after: afterValue,
      afterUnknown,
      sensitive,
      forcesReplacement: forced.has(key),
    };
  });
}

/** How a value is printed in a diff cell. */
export function formatPlanValue(value: unknown): string {
  if (value === null) {
    return 'null';
  }
  if (value === undefined) {
    return '-';
  }
  if (typeof value === 'string') {
    return `"${value}"`;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value, null, 2) ?? '-';
}

/** The plan's counts, restated as the sentence HCP puts above the list. */
export function planSummaryLine(changes: {
  add: number;
  change: number;
  destroy: number;
}): string {
  return `${String(changes.add)} to add, ${String(changes.change)} to change, ${String(changes.destroy)} to destroy`;
}

/** How many of a resource's attributes did not change. */
export function unchangedCount(attributes: readonly AttributeDiff[]): number {
  return attributes.filter((attribute) => attribute.kind === 'unchanged')
    .length;
}
