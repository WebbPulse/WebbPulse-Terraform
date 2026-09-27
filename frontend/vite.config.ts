import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react-swc';
import tailwindcss from '@tailwindcss/vite';

/**
 * Emits the Terraform service discovery document, pointing `modules.v1` at the
 * API host's registry. Skipped when the build has no API base URL.
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
        source: `${JSON.stringify({ 'modules.v1': `${origin}/v1/modules/` })}\n`,
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
