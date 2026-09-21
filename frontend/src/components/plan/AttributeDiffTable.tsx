/** The before and after attributes of one resource the plan touches. */

import { useState } from 'react';

import type { PlanResourceChange } from '../../api/runPlan';
import {
  attributeDiffs,
  formatPlanValue,
  unchangedCount,
  type AttributeDiff,
} from './planDiff';

/** Props for {@link AttributeDiffTable}. */
export interface AttributeDiffTableProps {
  change: PlanResourceChange;
}

/**
 * The resource's attributes, the unchanged ones folded away.
 *
 * Terraform's own output hides unchanged attributes behind a count, and a plan
 * with a hundred untouched fields is unreadable without that, so the same fold
 * is the default here and the count opens it.
 */
export function AttributeDiffTable({
  change,
}: AttributeDiffTableProps): React.ReactElement {
  const [showUnchanged, setShowUnchanged] = useState(false);
  const attributes = attributeDiffs(change);
  const hidden = unchangedCount(attributes);
  const shown = showUnchanged
    ? attributes
    : attributes.filter((attribute) => attribute.kind !== 'unchanged');

  if (attributes.length === 0) {
    return (
      <p className="px-3 py-2 text-xs text-text-faint">
        This resource has no attributes to compare.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {shown.length === 0 ? (
        <p className="px-3 py-2 text-xs text-text-faint">
          No attributes changed.
        </p>
      ) : (
        <dl className="divide-y divide-line">
          {shown.map((attribute) => (
            <AttributeRow key={attribute.key} attribute={attribute} />
          ))}
        </dl>
      )}
      {hidden === 0 ? null : (
        <button
          type="button"
          aria-expanded={showUnchanged}
          onClick={() => {
            setShowUnchanged((value) => !value);
          }}
          className="rounded px-3 py-1 text-xs text-text-muted hover:text-text-strong focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
        >
          {showUnchanged ? 'Hide' : 'Show'} {hidden} unchanged{' '}
          {hidden === 1 ? 'attribute' : 'attributes'}
        </button>
      )}
    </div>
  );
}

/** The marker and tone for each kind of attribute change. */
const KIND_CLASSES: Record<
  AttributeDiff['kind'],
  { glyph: string; tone: string }
> = {
  added: { glyph: '+', tone: 'text-add' },
  removed: { glyph: '-', tone: 'text-destroy' },
  changed: { glyph: '~', tone: 'text-change' },
  unchanged: { glyph: ' ', tone: 'text-text-faint' },
};

/** One attribute: its name, and its value before and after. */
function AttributeRow({
  attribute,
}: {
  attribute: AttributeDiff;
}): React.ReactElement {
  const kind = KIND_CLASSES[attribute.kind];
  return (
    <div
      data-testid="attribute-row"
      data-attribute={attribute.key}
      data-kind={attribute.kind}
      className="grid grid-cols-[1.25rem_minmax(0,11rem)_minmax(0,1fr)] gap-x-2 gap-y-1 px-3 py-1.5 text-xs"
    >
      <span aria-hidden="true" className={`text-center font-mono ${kind.tone}`}>
        {kind.glyph}
      </span>
      <dt className="min-w-0 truncate font-mono text-text-muted">
        {attribute.key}
        {attribute.forcesReplacement ? (
          <span
            data-testid="forces-replacement"
            className="mt-0.5 block font-sans text-[10px] font-medium text-replace"
          >
            forces replacement
          </span>
        ) : null}
      </dt>
      <dd className="min-w-0 font-mono break-all text-text">
        <AttributeValue attribute={attribute} />
      </dd>
    </div>
  );
}

/** The value cell: one value, or the old one arrowed to the new. */
function AttributeValue({
  attribute,
}: {
  attribute: AttributeDiff;
}): React.ReactElement {
  if (attribute.sensitive) {
    return <span className="text-text-faint">(sensitive value)</span>;
  }
  if (attribute.afterUnknown) {
    return (
      <span className="flex flex-wrap items-center gap-1.5">
        {attribute.kind === 'changed' && attribute.before !== undefined ? (
          <>
            <span className="text-text-muted line-through decoration-line-strong">
              {formatPlanValue(attribute.before)}
            </span>
            <Arrow />
          </>
        ) : null}
        <span className="text-text-faint">(known after apply)</span>
      </span>
    );
  }
  if (attribute.kind === 'changed') {
    return (
      <span className="flex flex-wrap items-center gap-1.5">
        <span className="text-text-muted line-through decoration-line-strong">
          {formatPlanValue(attribute.before)}
        </span>
        <Arrow />
        <span className="text-text-strong">
          {formatPlanValue(attribute.after)}
        </span>
      </span>
    );
  }
  if (attribute.kind === 'removed') {
    return (
      <span className="text-text-muted line-through decoration-line-strong">
        {formatPlanValue(attribute.before)}
      </span>
    );
  }
  return <span>{formatPlanValue(attribute.after)}</span>;
}

/** The mark between an old value and a new one. */
function Arrow(): React.ReactElement {
  return (
    <span aria-label="becomes" className="text-text-faint">
      to
    </span>
  );
}
