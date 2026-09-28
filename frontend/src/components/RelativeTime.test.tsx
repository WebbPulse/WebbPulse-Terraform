import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RelativeTime } from './RelativeTime';
import { RELATIVE_TIME_TICK_MS } from './useClock';

describe('RelativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-25T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('moves on from just now as time passes, with nothing else re-rendering', async () => {
    render(<RelativeTime iso="2026-09-25T12:00:00Z" />);

    expect(screen.getByText('just now')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000 + RELATIVE_TIME_TICK_MS);
    });

    expect(screen.getByText('1 min ago')).toBeInTheDocument();
    expect(screen.getByText('1 min ago')).toHaveAttribute(
      'datetime',
      '2026-09-25T12:00:00Z'
    );
  });

  it('keeps every instance on the same reading', async () => {
    render(
      <>
        <RelativeTime iso="2026-09-25T11:58:00Z" />
        <RelativeTime iso="2026-09-25T11:00:00Z" />
      </>
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(RELATIVE_TIME_TICK_MS);
    });

    expect(screen.getByText('2 min ago')).toBeInTheDocument();
    expect(screen.getByText('1 h ago')).toBeInTheDocument();
  });
});
