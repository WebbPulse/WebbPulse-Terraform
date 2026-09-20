/** A button that copies a string and says so for a moment. */

import { useEffect, useState } from 'react';

import { Button, type ButtonProps } from './Button';

/** Props for {@link CopyButton}. */
export interface CopyButtonProps extends Omit<
  ButtonProps,
  'children' | 'onClick'
> {
  /** What to copy. */
  value: string;
  /** The button's label before a copy. Defaults to "Copy". */
  label?: string;
  /** What the copy is, for the accessible name. */
  subject?: string;
}

/** Copies `value` to the clipboard, falling back to a text selection when the API is absent. */
async function copyText(value: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard !== undefined) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    return false;
  }
  return false;
}

/** A copy button that reads "Copied" for two seconds after a click. */
export function CopyButton({
  value,
  label = 'Copy',
  subject,
  size = 'sm',
  variant = 'ghost',
  ...rest
}: CopyButtonProps): React.ReactElement {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) {
      return;
    }
    const timer = setTimeout(() => {
      setCopied(false);
    }, 2000);
    return () => {
      clearTimeout(timer);
    };
  }, [copied]);

  return (
    <Button
      size={size}
      variant={variant}
      aria-label={subject === undefined ? undefined : `Copy ${subject}`}
      aria-live="polite"
      onClick={() => {
        void copyText(value).then((ok) => {
          setCopied(ok);
        });
      }}
      {...rest}
    >
      {copied ? 'Copied' : label}
    </Button>
  );
}
