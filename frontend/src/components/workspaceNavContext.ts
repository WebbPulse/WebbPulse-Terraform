/** The context a workspace page hands its name to the rail through. */

import { createContext, useContext } from 'react';

/** What the rail knows about the workspace being viewed. */
export interface WorkspaceNavState {
  /** The workspace name, once the page has loaded it. */
  name: string | null;
  setName: (name: string | null) => void;
}

/** Lets a workspace page hand its name to the rail above it. */
export const WorkspaceNavContext = createContext<WorkspaceNavState>({
  name: null,
  setName: () => undefined,
});

/** The rail state, a no-op outside the shell. */
export function useWorkspaceNav(): WorkspaceNavState {
  return useContext(WorkspaceNavContext);
}
