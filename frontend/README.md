# LedgerFlow Frontend

Next.js (App Router) dashboard for the LedgerFlow consolidation platform.

## Stack

- Next.js 16 · App Router · TypeScript
- Tailwind CSS 4 + shadcn/ui (base-nova) + Lucide React

## Routes

| Route | Purpose |
|---|---|
| `/` | Redirects to `/dashboard` |
| `/dashboard` | Overview |
| `/dashboard/entities` | Entity management (placeholder) |
| `/dashboard/source-data` | Source data (placeholder) |
| `/dashboard/runs` | Consolidation run history |
| `/dashboard/issues` | Issues from the latest run |
| `/dashboard/reports` | Consolidated financial statements |
| `/dashboard/agent-activity` | Agent activity (placeholder) |

The dashboard shell (dark navy sidebar, header, main area) is real. The
**Consolidation**, **Reports**, **Runs** and **Issues** pages load live results
from the backend API — statements, control figures, trial balance, eliminations
and the seven downloadable deliverables. The header polls the backend
`GET /api/health` endpoint and shows API connectivity. The Entities, Source
Data and Agent Activity pages remain honest placeholders.

## Develop

```bash
npm install
npm run dev       # http://localhost:3000
npm run lint      # eslint
npm run build     # production build (standalone output)
```

`NEXT_PUBLIC_API_URL` (default `http://localhost:8000`) points the app at the
backend. It is inlined at build time, so Docker passes it as a build argument.

## Docker

The multi-stage `Dockerfile` builds the standalone output and runs it from
`node:22-alpine` as a non-root user on port 3000. Build it as part of the
root Compose stack:

```bash
docker compose up --build   # from the repository root
```
