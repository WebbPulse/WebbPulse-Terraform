/** The control that switches between the dark default and the light theme. */

import { useTheme } from './themeContext';

/** Props for {@link ThemeToggle}. */
export interface ThemeToggleProps {
  className?: string;
}

/** A button that switches the theme, naming the theme it switches to. */
export function ThemeToggle({
  className = '',
}: ThemeToggleProps): React.ReactElement {
  const { theme, toggleTheme } = useTheme();
  const target = theme === 'dark' ? 'light' : 'dark';
  return (
    <button
      type="button"
      data-testid="theme-toggle"
      data-theme={theme}
      aria-label={`Switch to the ${target} theme`}
      title={`Switch to the ${target} theme`}
      onClick={toggleTheme}
      className={`inline-flex size-7 shrink-0 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-raised hover:text-text-strong focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none ${className}`}
    >
      {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}

/** The glyph shown while dark, standing for the light theme it switches to. */
function SunIcon(): React.ReactElement {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="size-4" fill="none">
      <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M8 1v1.75M8 13.25V15M15 8h-1.75M2.75 8H1m10.95-4.95-1.24 1.24M5.29 10.71l-1.24 1.24m0-7.9 1.24 1.24m5.42 5.42 1.24 1.24"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** The glyph shown while light, standing for the dark theme it switches to. */
function MoonIcon(): React.ReactElement {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="size-4" fill="none">
      <path
        d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5a5.75 5.75 0 1 0 7.1 7.1Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  );
}
