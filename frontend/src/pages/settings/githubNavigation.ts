/** The two ways the GitHub pages leave the SPA, kept apart so tests can stand in for them. */

/**
 * Posts the manifest to GitHub's new App page as a form, the only way GitHub accepts one.
 *
 * GitHub reads the manifest from a `manifest` form field on a top level POST, so
 * this builds that form and submits it rather than calling `fetch`.
 */
export function postManifest(
  actionUrl: string,
  manifest: Record<string, unknown>
): void {
  const form = document.createElement('form');
  form.method = 'post';
  form.action = actionUrl;
  form.style.display = 'none';
  const field = document.createElement('input');
  field.type = 'hidden';
  field.name = 'manifest';
  field.value = JSON.stringify(manifest);
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
}

/** Sends the browser to a GitHub URL, such as the App's install page. */
export function goTo(url: string): void {
  window.location.assign(url);
}
