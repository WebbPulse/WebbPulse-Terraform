import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react-swc';
import tailwindcss from '@tailwindcss/vite';

/**
 * Emits the Terraform service discovery document: the module and provider
 * registries on the API host, and `login.v1`, whose relative `authz` Terraform
 * resolves against this host's approve page. Skipped with no API base URL.
 */
function terraformDiscovery(apiBaseUrl: string | undefined): Plugin {
  return {
    name: 'terraform-discovery',
    apply: 'build',
    generateBundle() {
      if (!apiBaseUrl) return;
      const origin = apiBaseUrl.replace(/\/api\/v1\/?$/, '');
      this.emitFile({
        type: 'asset',
        fileName: '.well-known/terraform.json',
        source: `${JSON.stringify({
          'login.v1': {
            client: 'terraform-cli',
            grant_types: ['authz_code'],
            authz: '/oauth/authorize',
            token: `${origin}/v1/oauth/token`,
            ports: [10000, 10010],
          },
          'modules.v1': `${origin}/v1/modules/`,
          'providers.v1': `${origin}/v1/providers/`,
        })}\n`,
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_');
  return {
    plugins: [
      react(),
      tailwindcss(),
      terraformDiscovery(env.VITE_API_BASE_URL),
    ],
    server: {
      port: 5173,
      host: true,
      ...(mode === 'development'
        ? {
            proxy: {
              '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
                secure: false,
              },
            },
          }
        : {}),
    },
  };
});
