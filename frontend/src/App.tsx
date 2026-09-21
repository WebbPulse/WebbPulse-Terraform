/** The routed application, inside the one `AuthProvider` the API client shares. */

import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider, type AnyAuthClient } from '@webbpulse/auth/react';

import { api } from './api';
import { Layout, RequireAuth, RequireGuest } from './components';
import {
  NotFound,
  RunDetail,
  Runs,
  SignIn,
  Workspaces,
  workspaceRoutes,
} from './pages';
import './styles/globals.css';

/** The routes, so a test can mount them inside its own router. */
export function AppRoutes(): React.ReactElement {
  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          <RequireGuest>
            <SignIn />
          </RequireGuest>
        }
      />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Navigate to="/workspaces" replace />} />
        <Route path="/workspaces" element={<Workspaces />} />
        {workspaceRoutes()}
        <Route path="/runs" element={<Runs />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/**
 * The application.
 *
 * `AuthProvider` is given the API's own auth client, so the session the guards
 * read is the one the API client refreshes through rather than a second copy
 * with its own token.
 */
export function App(): React.ReactElement {
  return (
    <BrowserRouter>
      <AuthProvider client={api.getAuthClient() as unknown as AnyAuthClient}>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
