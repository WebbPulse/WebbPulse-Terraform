/** Placeholders shaped like the GitHub settings content, shown while it loads. */

/** One pulsing bar. */
export function SkeletonBar({
  className = '',
}: {
  className?: string;
}): React.ReactElement {
  return (
    <span
      aria-hidden="true"
      className={`block animate-pulse rounded bg-raised ${className}`}
    />
  );
}

/** A section frame with a heading and a few lines, labelled for assistive technology. */
export function SectionSkeleton({
  label,
  lines = 3,
}: {
  label: string;
  lines?: number;
}): React.ReactElement {
  return (
    <div
      role="status"
      aria-label={label}
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
    >
      <div className="space-y-2">
        <SkeletonBar className="h-4 w-40" />
        <SkeletonBar className="h-3 w-72 max-w-full" />
      </div>
      <div className="space-y-2">
        {Array.from({ length: lines }, (_, index) => (
          <SkeletonBar
            key={index}
            className={`h-3 ${index % 2 === 0 ? 'w-full' : 'w-2/3'}`}
          />
        ))}
      </div>
    </div>
  );
}

/** Rows standing in for a list or table body. */
export function RowsSkeleton({
  label,
  rows = 3,
}: {
  label: string;
  rows?: number;
}): React.ReactElement {
  return (
    <div
      role="status"
      aria-label={label}
      className="divide-y divide-line rounded-md border border-line"
    >
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-3 px-3 py-2.5">
          <SkeletonBar className="h-3 w-48 max-w-[60%]" />
          <SkeletonBar className="ml-auto h-3 w-20" />
        </div>
      ))}
    </div>
  );
}
