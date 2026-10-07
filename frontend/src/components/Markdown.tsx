/** A README rendered from Markdown, with raw HTML dropped and URLs resolved by the caller. */

import { useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Rewrites one URL from the source, or drops it by returning null.
 *
 * Runs after react-markdown's own sanitising, so it only ever sees safe schemes.
 */
export type UrlResolver = (url: string) => string | null;

/** Leaves every URL as written. */
const keep: UrlResolver = (url) => url;

/** The element map, closed over how links and images resolve. */
function components(
  resolveHref: UrlResolver,
  resolveSrc: UrlResolver
): Components {
  return {
    ...STATIC_COMPONENTS,
    a: ({ children, href }) => {
      const target = typeof href === 'string' ? resolveHref(href) : null;
      if (target === null) {
        return <span>{children}</span>;
      }
      return (
        <a
          href={target}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline-offset-2 hover:underline"
        >
          {children}
        </a>
      );
    },
    img: ({ src, alt }) => {
      const resolved = typeof src === 'string' ? resolveSrc(src) : null;
      if (resolved === null) {
        return alt ? <span className="text-text-muted">{alt}</span> : null;
      }
      return (
        <img
          src={resolved}
          alt={alt ?? ''}
          referrerPolicy="no-referrer"
          loading="lazy"
          className="inline max-w-full"
        />
      );
    },
  };
}

const STATIC_COMPONENTS: Components = {
  h1: ({ children }) => (
    <h2 className="mt-6 mb-3 border-b border-line pb-2 text-lg font-semibold text-text-strong first:mt-0">
      {children}
    </h2>
  ),
  h2: ({ children }) => (
    <h3 className="mt-6 mb-2 border-b border-line pb-1.5 text-base font-semibold text-text-strong first:mt-0">
      {children}
    </h3>
  ),
  h3: ({ children }) => (
    <h4 className="mt-5 mb-2 text-sm font-semibold text-text-strong first:mt-0">
      {children}
    </h4>
  ),
  h4: ({ children }) => (
    <h5 className="mt-4 mb-2 text-sm font-medium text-text-strong">
      {children}
    </h5>
  ),
  p: ({ children }) => <p className="my-3 leading-relaxed">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-3 list-disc space-y-1 pl-6">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-3 list-decimal space-y-1 pl-6">{children}</ol>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-line-strong pl-3 text-text-muted">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-5 border-line" />,
  pre: ({ children }) => (
    <pre className="my-3 overflow-auto rounded-md border border-code-line bg-code p-3 font-mono text-xs leading-relaxed text-code-text [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-code-text">
      {children}
    </pre>
  ),
  code: ({ children }) => (
    <code className="rounded bg-raised px-1 py-0.5 font-mono text-[0.85em] text-text-strong">
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        {children}
      </table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-line bg-raised px-2.5 py-1.5 font-medium text-text-strong">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-line px-2.5 py-1.5 align-top">{children}</td>
  ),
};

/** Props for {@link Markdown}. */
export interface MarkdownProps {
  source: string;
  /** The test id on the wrapper. */
  testId?: string;
  /** How a link's href resolves. Defaults to the href as written. */
  resolveHref?: UrlResolver;
  /** How an image's src resolves. Defaults to the src as written. */
  resolveSrc?: UrlResolver;
}

/** The rendered README. Raw HTML in the source is skipped rather than injected. */
export function Markdown({
  source,
  testId = 'module-readme',
  resolveHref = keep,
  resolveSrc = keep,
}: MarkdownProps): React.ReactElement {
  const mapped = useMemo(
    () => components(resolveHref, resolveSrc),
    [resolveHref, resolveSrc]
  );
  return (
    <div data-testid={testId} className="text-sm text-text">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={mapped}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
