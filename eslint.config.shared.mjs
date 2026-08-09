import eslint from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

/** Shared flat ESLint configuration for the TypeScript workspaces. */
export default tseslint.config(
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks
    },
    rules: {
      ...reactHooks.configs.flat.recommended.rules,
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }]
    }
  },
  {
    ignores: ['**/node_modules/**', '**/dist/**', '**/release/**', '**/.worktrees/**']
  }
)
