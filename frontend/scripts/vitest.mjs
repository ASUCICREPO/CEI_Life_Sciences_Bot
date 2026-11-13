// Cross-platform Vitest launcher used by `npm test` / `npm run test:watch`.
//
// Why: on Windows, a shell whose cwd has a lower-case drive letter (`c:\...`) makes Vitest resolve its own
// runner modules under two differently-cased paths, load them twice, and fail every suite with
// "Cannot read properties of undefined (reading 'config')". Setting `root` in vitest.config.ts does not help,
// because the main process has already imported Vitest by then. Starting Vitest in a child process whose cwd
// has an upper-case drive letter avoids it. On macOS/Linux the cwd is passed through unchanged.
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(import.meta.url)
const vitestBin = path.join(path.dirname(require.resolve('vitest/package.json')), 'vitest.mjs')

const normalizeDriveLetter = (p) => (process.platform === 'win32' ? p.replace(/^[a-z]:/, (d) => d.toUpperCase()) : p)

const child = spawn(process.execPath, [normalizeDriveLetter(vitestBin), ...process.argv.slice(2)], {
  cwd: normalizeDriveLetter(process.cwd()),
  stdio: 'inherit',
})

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}

child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal)
  else process.exit(code ?? 1)
})
