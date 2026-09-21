import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ThemeProvider } from './ThemeProvider';
import { useTheme } from './themeContext';
import { ThemeToggle } from './ThemeToggle';
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  otherTheme,
  readStoredTheme,
  resolveInitialTheme,
  storeTheme,
} from './theme';

/** A component that prints the theme it reads from the context. */
function ThemeReadout(): React.ReactElement {
  const { theme } = useTheme();
  return <span data-testid="readout">{theme}</span>;
}

describe('theme storage', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('defaults to dark with nothing stored', () => {
    expect(DEFAULT_THEME).toBe('dark');
    expect(resolveInitialTheme()).toBe('dark');
  });

  it('prefers a stored theme over the default', () => {
    storeTheme('light');
    expect(readStoredTheme()).toBe('light');
    expect(resolveInitialTheme()).toBe('light');
  });

  it('ignores a stored value that is not a theme', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'sepia');
    expect(readStoredTheme()).toBeNull();
    expect(resolveInitialTheme()).toBe('dark');
  });

  it('stays dark whatever the browser prefers', () => {
    const matchMedia = vi.fn().mockReturnValue({
      matches: true,
      media: '(prefers-color-scheme: light)',
    });
    vi.stubGlobal('matchMedia', matchMedia);

    expect(resolveInitialTheme()).toBe('dark');
    expect(matchMedia).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
  });

  it('survives storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('Storage is blocked.');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('Storage is blocked.');
    });

    expect(readStoredTheme()).toBeNull();
    expect(resolveInitialTheme()).toBe('dark');
    expect(() => {
      storeTheme('light');
    }).not.toThrow();
  });

  it('names the other theme', () => {
    expect(otherTheme('dark')).toBe('light');
    expect(otherTheme('light')).toBe('dark');
  });
});

describe('ThemeProvider', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
  });

  it('puts the theme on the document as it mounts', () => {
    render(
      <ThemeProvider>
        <ThemeReadout />
      </ThemeProvider>
    );

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(screen.getByTestId('readout')).toHaveTextContent('dark');
  });

  it('switches, stores and applies the theme from the toggle', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
        <ThemeReadout />
      </ThemeProvider>
    );

    const toggle = screen.getByTestId('theme-toggle');
    expect(toggle).toHaveAttribute('aria-label', 'Switch to the light theme');

    await userEvent.click(toggle);

    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    expect(screen.getByTestId('readout')).toHaveTextContent('light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
    expect(toggle).toHaveAttribute('aria-label', 'Switch to the dark theme');

    await userEvent.click(toggle);
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  });

  it('starts from what a previous visit stored', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');

    render(
      <ThemeProvider>
        <ThemeReadout />
      </ThemeProvider>
    );

    expect(screen.getByTestId('readout')).toHaveTextContent('light');
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  });

  it('reads as dark outside a provider rather than throwing', () => {
    render(<ThemeReadout />);

    expect(screen.getByTestId('readout')).toHaveTextContent('dark');
  });
});
