import { reactConfig } from '@webbpulse/eslint-config/react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import prettier from 'eslint-plugin-prettier';

export default [
  ...reactConfig({
    project: ['./tsconfig.app.json', './tsconfig.node.json'],
    tsconfigRootDir: import.meta.dirname,
    plugins: { 'react-hooks': reactHooks, 'react-refresh': reactRefresh },
  }),
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { prettier },
    rules: {
      'prettier/prettier': 'error',
    },
  },
];
