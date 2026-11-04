import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// NOTE: run tests via `npm test` / `npm run test:watch`, which go through scripts/vitest.mjs. That launcher
// normalizes a lower-case Windows drive letter (`c:\...`) in the cwd, which otherwise makes Vitest load its
// runner twice and fail every suite. Calling `npx vitest` directly bypasses it.

// ADR: separate vitest config | Rationale: the app's vite.config.ts carries the Tailwind plugin and dev
// proxies, neither of which unit tests need | Alternative: a `test` block in vite.config.ts rejected — it would
// load Tailwind for every test run
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    unstubGlobals: true,
  },
})
