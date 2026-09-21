/** A monospace block with a copy button pinned to its corner. */

import { CopyButton } from './CopyButton';

/** Props for {@link CodeBlock}. */
export interface CodeBlockProps {
  code: string;
  /** What the code is, for the copy button's accessible name and the test id. */
  subject: string;
  className?: string;
}

/** A scrollable code block with a copy action. */
export function CodeBlock({
  code,
  subject,
  className = '',
}: CodeBlockProps): React.ReactElement {
  return (
    <div
      className={`relative rounded-md border border-code-line bg-code ${className}`}
    >
      <div className="absolute top-1.5 right-1.5">
        <CopyButton value={code} subject={subject} variant="inverse" />
      </div>
      <pre
        data-testid="code-block"
        data-subject={subject}
        className="max-h-96 overflow-auto p-3 pr-20 font-mono text-xs leading-relaxed text-code-text"
      >
        {code}
      </pre>
    </div>
  );
}
