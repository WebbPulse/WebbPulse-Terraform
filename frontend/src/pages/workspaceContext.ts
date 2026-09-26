/** What the workspace frame shares with the pages inside it. */

import { useOutletContext } from 'react-router-dom';

import type { ConfigVersion, Run, RunRoleCheck, Workspace } from '../api';
import type { SetupStep } from './workspace/setup';

/** The refetch keys for a workspace, its versions, its runs and its run role check. */
export interface WorkspaceKeys {
  workspace: string;
  versions: string;
  runs: string;
  runRoleCheck: string;
}

/** What every workspace page reads from the frame through the outlet. */
export interface WorkspaceContext {
  workspace: Workspace;
  versions: ConfigVersion[];
  runs: Run[];
  /** The live run role check, or null until it has answered for the saved ARN. */
  runRoleCheck: RunRoleCheck | null;
  steps: SetupStep[];
  /** Whether the version and run lists have both arrived. */
  settled: boolean;
  /** Whether every setup step is done, so the checklist is gone. */
  setupComplete: boolean;
  versionsQuery: { isLoading: boolean; error: unknown };
  runsQuery: { isLoading: boolean; error: unknown };
  keys: WorkspaceKeys;
  /** Opens the new run dialog. */
  openNewRun: () => void;
}

/** The workspace the page sits in. Throws outside the workspace routes. */
export function useWorkspace(): WorkspaceContext {
  const context = useOutletContext<WorkspaceContext | undefined>();
  if (context === undefined) {
    throw new Error('useWorkspace must be used inside the workspace routes.');
  }
  return context;
}

/** The workspace the page sits in, or null when the page is mounted on its own. */
export function useOptionalWorkspace(): WorkspaceContext | null {
  return useOutletContext<WorkspaceContext | undefined>() ?? null;
}

/** The refetch keys for a workspace id. */
export function workspaceKeys(workspaceId: string): WorkspaceKeys {
  return {
    workspace: `workspace:${workspaceId}`,
    versions: `config-versions:${workspaceId}`,
    runs: `runs:${workspaceId}`,
    runRoleCheck: `run-role-check:${workspaceId}`,
  };
}
