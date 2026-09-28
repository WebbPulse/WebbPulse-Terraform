/** A module readme rendered from Markdown, with raw HTML dropped. */

import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

const COMPONENTS: Components = {
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
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline-offset-2 hover:underline"
    >
      {children}
    </a>
  ),
  img: ({ src, alt }) => (
    <img
      src={typeof src === 'string' ? src : undefined}
      alt={alt ?? ''}
      referrerPolicy="no-referrer"
      loading="lazy"
      className="inline max-w-full"
    />
  ),
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
}

/** The rendered readme. Raw HTML in the source is skipped rather than injected. */
export function Markdown({ source }: MarkdownProps): React.ReactElement {
  return (
    <div data-testid="module-readme" className="text-sm text-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={COMPONENTS}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
