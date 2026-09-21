/** The theme context and the hook that reads it, kept apart from the provider component. */

import { createContext, useContext } from 'react';

import type { Theme } from './theme';

/** What {@link useTheme} hands back. */
export interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

/** The context the provider fills, null until one is mounted. */
export const ThemeContext = createContext<ThemeState | null>(null);

/**
 * The current theme and the ways to change it.
 *
 * Falls back to the dark default outside a provider, so a component rendered
 * on its own in a test still has a theme to read rather than throwing.
 */
export function useTheme(): ThemeState {
  const value = useContext(ThemeContext);
  if (value === null) {
    return {
      theme: 'dark',
      setTheme: () => undefined,
      toggleTheme: () => undefined,
    };
  }
  return value;
}
