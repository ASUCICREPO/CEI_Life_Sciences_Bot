import { defineConfig, loadEnv, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// ADR: dev proxy for the deployed REST API, keyed off CEI_API_BASE_URL (the ApiBaseUrl stack output,
// https://<id>.execute-api.<region>.amazonaws.com/<stage>) | Rationale: local config.json has no absolute
// apiBaseUrl, so the clients call same-origin /api/<route> and the dev server forwards it — no CORS from
// localhost (gateway errors carry only the site origin). Not VITE_-prefixed: dev-server only, never bundled.
function apiGatewayProxy(apiBaseUrl: string | undefined): Record<string, ProxyOptions> {
  if (!apiBaseUrl) return {}
  const url = new URL(apiBaseUrl)
  const stagePath = url.pathname.replace(/\/+$/, '')
  const proxy: ProxyOptions = {
    target: url.origin,
    changeOrigin: true,
    secure: true,
    // /api/sessions/... -> /<stage>/sessions/...
    rewrite: (path) => path.replace(/^\/api/, stagePath),
  }
  return {
    '/api/sessions': proxy,
    '/api/models': proxy,
    '/api/settings': proxy,
    '/api/account': proxy,
    '/api/profile': proxy,
  }
}

// Dev proxies for NCBI. The public BLAST endpoint does not send CORS headers,
// so a browser blocks direct calls and the app falls back to illustrative
// data. Proxying through the dev server makes these calls same-origin, so the
// client's "paste a sequence → get real matching sequences in-app" BLAST flow
// works end-to-end without the user leaving the app.
export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), '')
  if (command === 'serve' && !env.CEI_API_BASE_URL) {
    console.warn(
      '[cei] CEI_API_BASE_URL is not set: /api/sessions, /api/models and /api/settings are NOT proxied, so chat history, resume and model/settings will fail locally. ' +
        'Set it to the ApiBaseUrl stack output in frontend/.env.local (see README "Local development").',
    )
  }
  return {
    plugins: [
      react(),
      tailwindcss(),
    ],
    build: {
      // ADR: never inline fonts as data: URLs | Rationale: the CSP sets font-src 'self', which blocks data: fonts; Vite inlines assets under 4 KB by default and some Fontsource subsets are that small | Alternative: adding data: to font-src rejected — widens the CSP
      assetsInlineLimit: (filePath) => (/\.(woff2?|ttf|otf)$/.test(filePath) ? false : undefined),
    },
    server: {
      proxy: {
        '/ncbi-blast': {
          target: 'https://blast.ncbi.nlm.nih.gov',
          changeOrigin: true,
          secure: true,
          rewrite: (path) => path.replace(/^\/ncbi-blast/, '/Blast.cgi'),
        },
        '/ncbi-eutils': {
          target: 'https://eutils.ncbi.nlm.nih.gov',
          changeOrigin: true,
          secure: true,
          rewrite: (path) => path.replace(/^\/ncbi-eutils/, '/entrez/eutils'),
        },
        ...apiGatewayProxy(env.CEI_API_BASE_URL),
        // ADR: proxy local AgentCore runtimes by name | op-based /invocations
        // contract (B94-B98); long timeout covers run/resume tool calls.
        '/api/agents/research': {
          target: 'http://localhost:8080',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api\/agents\/research/, '/invocations'),
          timeout: 600_000,
          proxyTimeout: 600_000,
        },
        '/api/agents/drafting': {
          target: 'http://localhost:8081',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api\/agents\/drafting/, '/invocations'),
          timeout: 600_000,
          proxyTimeout: 600_000,
        },
        '/api/agents/analysis': {
          target: 'http://localhost:8083',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api\/agents\/analysis/, '/invocations'),
          timeout: 600_000,
          proxyTimeout: 600_000,
        },
        '/api/agents/pipeline': {
          target: 'http://localhost:8084',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api\/agents\/pipeline/, '/invocations'),
          timeout: 600_000,
          proxyTimeout: 600_000,
        },
      },
    },
  }
})
