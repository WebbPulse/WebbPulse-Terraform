/** A modal dialog: an overlay, a titled panel, Escape to close and focus kept inside. */

import { useEffect, useId, useRef, type ReactNode } from 'react';

/** Props for {@link Dialog}. */
export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** One sentence under the title. */
  description?: string;
  children: ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** A modal dialog. Renders nothing while closed. */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
}: DialogProps): React.ReactElement | null {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    if (!open) {
      return;
    }
    const previous = document.activeElement;
    const first = panel.current?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab' || panel.current === null) {
        return;
      }
      const focusable = Array.from(
        panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)
      );
      if (focusable.length === 0) {
        return;
      }
      const start = focusable[0];
      const end = focusable[focusable.length - 1];
      if (start === undefined || end === undefined) {
        return;
      }
      if (event.shiftKey && document.activeElement === start) {
        event.preventDefault();
        end.focus();
      } else if (!event.shiftKey && document.activeElement === end) {
        event.preventDefault();
        start.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      if (previous instanceof HTMLElement) {
        previous.focus();
      }
    };
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-surface-900/40 p-4 pt-[12vh] backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description === undefined ? undefined : descriptionId}
        className="w-full max-w-md rounded-lg border border-line bg-panel shadow-xl"
      >
        <div className="border-b border-line px-5 py-4">
          <h2 id={titleId} className="text-base font-semibold text-text-strong">
            {title}
          </h2>
          {description === undefined ? null : (
            <p id={descriptionId} className="mt-1 text-sm text-text-muted">
              {description}
            </p>
          )}
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
