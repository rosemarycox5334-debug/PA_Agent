# PA Agent WebUI — Frontend (Vite + Vue 3 + TypeScript)

> Modern SPA shell that replaces the legacy `pa_agent/web/static/index.html`
> build while keeping the FastAPI backend (`pa_agent/web/main.py`) as the
> single source of truth for HTTP and SSE.

## Layout

```
webui-frontend/
├── index.html               # Vite entry document
├── package.json             # vue 3, vite 5, typescript, @vitejs/plugin-vue
├── tsconfig.json            # TS strict mode (all strict flags enabled)
├── vite.config.ts           # base=/static/webui/, dev :5173, /api proxy
└── src/
    ├── main.ts              # createApp + router + global styles
    ├── App.vue              # header / tabs / status bar shell
    ├── env.d.ts             # *.vue module declaration
    ├── api/
    │   └── client.ts        # unified fetch + typed endpoint helpers
    ├── stores/              # Vue 3 reactive() stores
    │   ├── settings.ts      # settings + derived status/label/progress
    │   ├── frame.ts         # kline snapshot + EventSource lifecycle
    │   ├── decision.ts      # stage-2 decision + reasoning text
    │   └── stream.ts        # SSE reader + event dispatch
    ├── router/
    │   └── index.ts         # hash-history router with tab routes
    ├── styles/
    │   ├── tokens.css       # CSS variables ported from app.css
    │   └── global.css       # minimal base styles (reset + pills + dots)
    └── views/
        ├── TerminalView.vue # toolbar + chart + followup
        ├── DecisionView.vue # decision summary + trace + raw JSON
        ├── RecordsView.vue  # historical record replay
        └── SettingsView.vue # settings editor
```

## Develop

```bash
cd pa_agent/webui-frontend
npm install
npm run dev
```

The dev server runs on `http://localhost:5173` and proxies `/api/*` to
`http://localhost:8080` where the FastAPI process should be running.

## Build

```bash
npm run build
```

Produces `pa_agent/webui-frontend/dist/`. On the next server start,
`pa_agent/web/main.py` will detect the directory and mount the SPA at
`/webui` (with `html=True` for the standard SPA fallback to
`dist/index.html`).

The legacy UI under `pa_agent/web/static/` remains mounted at `/` and is
unaffected, so the new and old SPAs can be compared side by side until
the Vite build is promoted to default.

## Strict TypeScript

`tsconfig.json` enables every strict flag — `strict`, `noUnusedLocals`,
`noUnusedParameters`, `noImplicitReturns`, `noFallthroughCasesInSwitch`,
`forceConsistentCasingInFileNames`. Run `npm run type-check` (which uses
`vue-tsc --noEmit`) before sending a PR.
