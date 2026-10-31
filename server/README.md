# KaraokeGen setup helper

One-time auto-setup, like the KaraokeNow ReVanced patch: a user pastes
their `modal token set ...` command into the Android app, and this
helper deploys THEIR OWN KaraokeGen backend into their Modal account
and hands back its URL + API key. The phone can't run `modal deploy`
itself, so this tiny service does it server-side.

## How it works

- `POST /setup/start {token_id, token_secret}` → `{job_id}`
  (the token is verified with `modal token info` first — junk tokens
  are rejected in seconds, with no deploy billed)
- `GET /setup/status/{job_id}` → `{state, stage, progress, url?, api_key?, error?}`
- `GET /health` → `{status: "ok"}`

The deploy runs in a spawned function with the token ONLY in the
subprocess environment. A fresh random API key is generated per setup
and written to a `.env` next to `modal_app.py` before deploying, so
the backend's `modal.Secret.from_dotenv()` picks it up. The token is
never logged, never stored, and never used for anything except that
one deploy.

Abuse guards: 3 setup starts/hour per IP, 50/day globally, max 2
concurrent deploys, 30 status reads/min per IP, 30-minute deploy cap.

## Deploy the helper

Needs your Modal token as repo secrets (`MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET`), then run the **Deploy Modal setup helper**
workflow (manual trigger). It lives at:

`https://<your-workspace>--karaokegen-setup.us-east.modal.run`

Set the repo variable `KARAOKEGEN_SETUP_URL` to that address so the
workflow's smoke check can verify `/health`.

Or deploy by hand: `modal deploy server/setup_app.py::setup_app`

The Android app talks to the helper at the URL in
`SetupClient.HELPER_URL` — update it if you deploy under a different
workspace.
