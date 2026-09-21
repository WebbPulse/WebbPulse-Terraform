/** The provider that keeps the document in step with the theme state. */

import { useCallback, useState } from 'react';
import type { ReactNode } from 'react';

import {
  applyTheme,
  otherTheme,
  resolveInitialTheme,
  storeTheme,
  type Theme,
} from './theme';
import { ThemeContext } from './themeContext';

/**
 * The theme state, and the setter that stores and applies in one step.
 *
 * The attribute is written in the same call that sets the state rather than in
 * an effect, so the paint that shows the new colours is the one that shows the
 * new state and there is no flash of the old theme between them.
 */
export function ThemeProvider({
  children,
}: {
  children: ReactNode;
}): React.ReactElement {
  const [theme, setThemeState] = useState<Theme>(() => {
    const initial = resolveInitialTheme();
    applyTheme(initial);
    return initial;
  });

  const setTheme = useCallback((next: Theme): void => {
    setThemeState(next);
    applyTheme(next);
    storeTheme(next);
  }, []);

  const toggleTheme = useCallback((): void => {
    setThemeState((current) => {
      const next = otherTheme(current);
      applyTheme(next);
      storeTheme(next);
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}
