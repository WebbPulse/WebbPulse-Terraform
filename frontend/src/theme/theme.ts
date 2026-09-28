/** The colour theme: dark by default, light as a stored opt-in. */

/** The two themes the app ships. */
export type Theme = 'dark' | 'light';

/** The theme a browser with nothing stored gets, whatever the OS prefers. */
export const DEFAULT_THEME: Theme = 'dark';

/** The localStorage key the choice is kept under. */
export const THEME_STORAGE_KEY = 'webbpulse-terraform-theme';

/** Whether a stored string is one of the themes. */
function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light';
}

/**
 * The stored theme, or null when nothing valid is stored.
 *
 * Storage throws in a private window and when site data is blocked, so every
 * read is guarded and a failure reads as "nothing stored".
 */
export function readStoredTheme(): Theme | null {
  try {
    const stored = globalThis.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

/** Stores the theme, ignoring a storage that refuses to be written to. */
export function storeTheme(theme: Theme): void {
  try {
    globalThis.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    return;
  }
}

/**
 * The theme to start in: the stored one, else dark.
 *
 * The OS preference is deliberately not consulted. Dark is this product's
 * default and light is an explicit choice a person makes and keeps.
 */
export function resolveInitialTheme(): Theme {
  return readStoredTheme() ?? DEFAULT_THEME;
}

/** Puts the theme on the document element, which is what the tokens key off. */
export function applyTheme(theme: Theme): void {
  globalThis.document.documentElement.setAttribute('data-theme', theme);
}

/** The theme the toggle switches to. */
export function otherTheme(theme: Theme): Theme {
  return theme === 'dark' ? 'light' : 'dark';
}
