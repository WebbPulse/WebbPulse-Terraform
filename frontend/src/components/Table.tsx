/** The bordered table shell, and the cells and rows every list shares. */

import type { ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react';

/** Props for {@link Table}. */
export interface TableProps {
  children: ReactNode;
  className?: string;
  /** The accessible name of the table. */
  label?: string;
}

/** A table inside a rounded border, with a muted header row. */
export function Table({
  children,
  className = '',
  label,
}: TableProps): React.ReactElement {
  return (
    <div
      className={`overflow-x-auto rounded-lg border border-line bg-panel ${className}`}
    >
      <table aria-label={label} className="w-full text-left text-sm">
        {children}
      </table>
    </div>
  );
}

/** A header cell. */
export function Th({
  className = '',
  children,
  ...rest
}: ThHTMLAttributes<HTMLTableCellElement>): React.ReactElement {
  return (
    <th
      scope="col"
      className={`bg-bg px-3 py-2 text-xs font-medium whitespace-nowrap text-text-muted ${className}`}
      {...rest}
    >
      {children}
    </th>
  );
}

/** A body cell. */
export function Td({
  className = '',
  children,
  ...rest
}: TdHTMLAttributes<HTMLTableCellElement>): React.ReactElement {
  return (
    <td className={`px-3 py-2 align-middle ${className}`} {...rest}>
      {children}
    </td>
  );
}

/** Props for {@link Tr}. */
export interface TrProps {
  children: ReactNode;
  className?: string;
}

/** A body row with the divider and hover every list shares. */
export function Tr({ children, className = '' }: TrProps): React.ReactElement {
  return (
    <tr
      className={`border-t border-line transition-colors hover:bg-raised/60 ${className}`}
    >
      {children}
    </tr>
  );
}
