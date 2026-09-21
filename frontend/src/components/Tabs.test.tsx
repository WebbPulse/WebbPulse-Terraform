import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { Tabs } from './Tabs';

function Harness(): React.ReactElement {
  const [value, setValue] = useState<'one' | 'two' | 'three'>('one');
  return (
    <Tabs
      label="Sections"
      tabs={[
        { id: 'one', label: 'One' },
        { id: 'two', label: 'Two', disabled: true },
        { id: 'three', label: 'Three' },
      ]}
      value={value}
      onChange={setValue}
    />
  );
}

describe('Tabs', () => {
  it('moves with the arrow keys and skips disabled tabs', async () => {
    render(<Harness />);
    const first = screen.getByRole('tab', { name: 'One' });
    first.focus();
    await userEvent.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Three' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByRole('tab', { name: 'Three' })).toHaveFocus();
    await userEvent.keyboard('{ArrowRight}');
    expect(first).toHaveAttribute('aria-selected', 'true');
    await userEvent.keyboard('{End}');
    expect(screen.getByRole('tab', { name: 'Three' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
  });

  it('selects on click and leaves disabled tabs alone', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('tab', { name: 'Three' }));
    expect(screen.getByRole('tab', { name: 'Three' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByRole('tab', { name: 'Two' })).toBeDisabled();
  });
});
