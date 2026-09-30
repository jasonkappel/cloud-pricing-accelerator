# Cloud Pricing Accelerator Web

TypeScript React review shell for the public-list run-rate benchmark.

## Run locally

Use Node.js 24 or newer. After the main build-validation step installs dependencies, run:

```powershell
npm run dev
```

Copy `.env.sample` to `.env` or set `VITE_API_BASE_URL` explicitly before running Vite. Production builds
load `/config.json` from `server.mjs` instead, so the generated static assets remain host-agnostic. In
Azure, `server.mjs` forwards `/api` to `API_UPSTREAM_URL` with the signed-in user's access token (App
Service Authentication supplies it), strips client-supplied identity and forwarding headers, and serves an
empty `apiBaseUrl` so the browser calls its own origin. On a lapsed session the browser refreshes once
through `/.auth/refresh`, then goes to sign-in. The HashRouter keeps navigation host-agnostic. The web app
displays only API-calculated public pricing.
When the API enables `PRESENT_AWS_PRICING`, the Comparisons page shows both clouds' List,
Savings Plan, and Reservation scenarios together with commitment assumptions and breakeven
sensitivity; it does not calculate or infer contracted rates in the browser.
