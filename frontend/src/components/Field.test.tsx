import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Field, INPUT_CLASS } from './Field';

describe('Field', () => {
  it('links the label, hint and error to the control', () => {
    render(
      <Field label="Role ARN" hint="Keep the prefix." error="Too short.">
        {(control) => <input {...control} className={INPUT_CLASS} />}
      </Field>
    );
    const input = screen.getByLabelText('Role ARN');
    expect(input).toHaveAccessibleDescription('Keep the prefix. Too short.');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Too short.');
  });

  it('marks a clean control valid with no description', () => {
    render(<Field label="Name">{(control) => <input {...control} />}</Field>);
    const input = screen.getByLabelText('Name');
    expect(input).toHaveAttribute('aria-invalid', 'false');
    expect(input).not.toHaveAttribute('aria-describedby');
  });
});
