# tradeflow dashboard

React 18 + TypeScript (strict), Vite, Tailwind, TanStack Query, Lightweight Charts.

```
npx -y npm@11.21.1 ci     # Node 22; npm 10 crashes on this dependency tree
npm run dev               # http://localhost:5173, proxies /api and /ws to the backend on :8000
npm run lint && npm run typecheck && npm test && npm run build
```

Log in with the backend's `ADMIN_TOKEN` (kept in sessionStorage for the tab only).
