/** Where a VCS run came from: the trigger, the commit and the pull request, linked to GitHub. */

import type { Run } from '../api';
import { runSource } from './runText';

/** Props for {@link RunSourceLine}. */
export interface RunSourceLineProps {
  run: Pick<Run, 'vcs'>;
  /** Lifts the links above a row's stretched link so they stay clickable. */
  raised?: boolean;
}

/** The trigger sentence, the short sha and the pull request, or nothing for a run started by hand. */
export function RunSourceLine({
  run,
  raised = false,
}: RunSourceLineProps): React.ReactElement | null {
  const source = runSource(run);
  if (source === null) {
    return null;
  }
  const linkClass = `${raised ? 'relative z-10 ' : ''}hover:text-accent hover:underline`;
  return (
    <span
      data-testid="run-source"
      className="inline-flex flex-wrap items-center gap-x-3"
    >
      <span>{source.trigger}</span>
      <a
        href={source.commitUrl}
        target="_blank"
        rel="noreferrer"
        title={source.sha}
        aria-label={`Commit ${source.shortSha} on GitHub`}
        className={`font-mono ${linkClass}`}
      >
        {source.shortSha}
      </a>
      {source.prUrl === null ? null : (
        <a
          href={source.prUrl}
          target="_blank"
          rel="noreferrer"
          className={linkClass}
        >
          PR #{source.prNumber}
        </a>
      )}
    </span>
  );
}
