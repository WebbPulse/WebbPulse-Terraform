/** How sign-in leaves the SPA for the device approval page, kept apart so tests can stand in for it. */

/** Sends the browser to the identity service's device approval page. */
export function leaveForDeviceApproval(url: string): void {
  window.location.replace(url);
}
