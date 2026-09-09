# 0004 — Attach a Railway persistent volume at `/app/data`

**Date:** 2026-06-01
**Status:** Accepted

## Context

The design called for the Railway container to run with a "persistent
volume" so the operator-state files under `data/` survive deploys. The
wiring never happened: the first deploy went up with the default container
filesystem and no volume attached. The evidence:

- `railway volume list` returned `{"volumes": []}`.
- The repo's `data/.gitkeep` (baked into the image at build time) was still
  visible at `/app/data/.gitkeep` on Railway. A mounted volume would overlay
  the image's contents at that path — the file's presence proves no volume.
- `railway.json` declared only the Dockerfile builder; no volume config.

What lives under `data/` and would have been lost on every deploy:

| Path                       | Written by                         | Recoverability if lost                |
| -------------------------- | ---------------------------------- | ------------------------------------- |
| `data/transcripts/*.jsonl` | `outside_line.transcripts.TranscriptWriter`     | Twilio has the recording, not the text |
| `data/sessions/*.session`  | Telethon                           | Re-warm with OTP (acceptable)         |
| `data/contacts.json`       | hand-seeded                        | Gitignored; operator re-seeds via `scripts/contacts.py` |

The lossy item in that list is the transcripts — relied on by operator
tooling (`./scripts/archive.py`, `./scripts/transcripts.py`).

## Decision

Attach a single Railway volume mounted at **`/app/data`** to the app's
service in the `production` environment:

```sh
railway volume add --service <service> --mount-path /app/data
```

The mount path matches `WORKDIR=/app` + the in-code relative `data/` paths
in `src/outside_line/{transcripts.py,config.py}`, so **no code
change is needed**.

- **Size:** Railway's platform default. The CLI has no `--size` flag, and
  actual usage is in kilobytes per call (text JSONL + small JSON).
  Recordings stay on Twilio, so the volume never holds audio.
- **Per-environment:** one volume, one environment (production). Staging
  would get its own when introduced.
- **Lifecycle:** the volume outlives deploys and outlives a `detach`. It is
  only destroyed by an explicit `railway volume delete`.

## Consequences

- **First-attach causes a redeploy** (~1–2 min outage). Twilio inbound calls
  fail during that window. Acceptable given single-user low-volume.
- **`/app/data/.gitkeep` from the image is no longer visible** — the volume
  overlay hides it. Not a functional issue; the app's `mkdir(parents=True,
  exist_ok=True)` calls create whatever it needs.
- **No `VOLUME /app/data` directive in the Dockerfile** and no entry in
  `railway.json`. Railway's volume model is platform-side; the build config
  doesn't declare it. This ADR is the durable record.
- **Backups remain operator-driven** via `./scripts/archive.py pull`, which
  mirrors transcripts to local. Combined with Twilio's
  recording archive, that's the MVP backup story. First-class snapshots are
  out of scope for now.
- **Rollback path:** `railway volume detach --service <service>` + redeploy
  returns the service to ephemeral fs without destroying the volume. Data
  remains on the detached volume; re-attach to restore.
- **Encryption at rest is still not enabled** (a known post-MVP item). Volume contents (session file, contacts
  JSON) are sensitive but unencrypted, same threat model as before.

## Operational notes

- `railway volume files {list,download,upload,delete}` takes paths
  **relative to the volume root**, not the container's view. So
  `data/transcripts/<sid>.jsonl` in the container is
  `/transcripts/<sid>.jsonl` to `railway volume files`. `scripts/archive.py`
  continues to use `railway ssh` (container view),
  which is unaffected by the volume — same paths, just now durable.
