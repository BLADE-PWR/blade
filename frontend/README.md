# frontend

The operator app (module 8 in `docs/opis_projektu.md`): React + TypeScript, built with Vite. It talks only
to the [gateway](../services/gateway/README.md): REST for control, one WebSocket for live data.

For now it is a walking skeleton: pick a gateway session (or streams and worlds), open the WebSocket, see
the video previews and how many messages of each type arrive. The map comes next.

It is not part of the uv workspace; it has its own `package.json`. Needs Node.js 22 or newer
(Fedora: `sudo dnf install nodejs`, Windows: `winget install OpenJS.NodeJS.LTS`).

## Run

With the server running (`uv run python tools/dev.py --map altis`) and, for data, a replay:
```bash
cd frontend
```
```bash
npm install
```
```bash
npm run dev
```

Open http://localhost:5173. In dev, Vite forwards `/api`, `/sessions`, `/detectors`, `/streams` and the
`/ws` WebSocket to the gateway on `127.0.0.1:8000`. For a gateway on another machine:
```bash
GATEWAY_URL=http://<server-ip>:8000 npm run dev
```

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Dev server with the gateway proxy |
| `npm run typecheck` | TypeScript check only |
| `npm run build` | Typecheck, then a production build in `dist/` |
| `npm run preview` | Serve `dist/` locally |

## Code

| File | What it is |
|---|---|
| `src/gateway.ts` | WebSocket client: JSON messages and `ZPIF` video previews (`b"ZPIF"` + uint32 BE header length + header JSON + JPEG) |
| `src/App.tsx` | Session picker and the live view |
| `vite.config.ts` | The dev proxy to the gateway |
