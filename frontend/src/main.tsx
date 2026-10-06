import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// ADR: self-hosted fonts via Fontsource | Rationale: the CSP allows only 'self' for styles and fonts, so Vite bundles the woff2 files into dist/ at build time | Alternative: loading from fonts.googleapis.com rejected — blocked by CSP and sends user IPs to Google
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './styles.css'
import App from './App'
import { loadRuntimeConfig } from './lib/auth/config'

const rootElement = document.getElementById('root')!
const root = createRoot(rootElement)

// ADR: block first render on /config.json | the app cannot build a UserManager without it
loadRuntimeConfig()
  .then(() => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
  })
  .catch((error: unknown) => {
    root.render(
      <div className="h-screen flex items-center justify-center bg-surface-primary">
        <div className="max-w-sm text-center space-y-2">
          <p className="text-sm text-text-primary font-medium">Unable to load app configuration.</p>
          <p className="text-xs text-text-secondary">
            {error instanceof Error ? error.message : 'Unknown error.'}
          </p>
        </div>
      </div>,
    )
  })
