import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
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
