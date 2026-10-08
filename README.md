# KIR Auto-Clipper

A personal, automated replacement for Opus Clip, built for the Keepingit_Rael streams.

When OBS finishes a recording in the watched folder, the clipper:

1. waits until the file has stopped growing,
2. transcribes it with word-level timestamps,
3. has Claude find self-contained moments and score them 0-99,
4. renders each one as a 1080x1920 clip with animated captions,
5. auto-schedules clips scoring 90 or higher, sends 60-89 to a review queue, and drops anything below 60,
6. posts through Upload-Post on a spread-out schedule, and
7. 90 days after a clip goes out, re-edits it with a new look and new hook and queues it again.

It runs on your Windows PC in the background. The review dashboard is at **http://127.0.0.1:8765**.

---

## 1. Setup (once)

You need **Python 3.11+**, **ffmpeg**, and **git** on Windows.

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Git.Git
# open a NEW PowerShell window so PATH updates, then:
git clone <this repo> $HOME\kir-clipper
cd $HOME\kir-clipper
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

`install.ps1` creates a virtual environment, installs everything (including local Whisper and face
tracking), adds NVIDIA's CUDA libraries if you have an NVIDIA GPU, creates `.env`, and runs a setup check.

### API keys

Open `.env` in Notepad and fill in:

| Key | Where to get it | Needed for |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys | Picking and scoring clips (required) |
| `UPLOAD_POST_API_KEY` | Upload-Post dashboard → API keys | Posting (not needed while in dry-run) |
| `ALERT_WEBHOOK_URL` | Discord channel → Integrations → Webhooks (optional) | Daily summary and failure alerts on your phone |
| `OPENAI_API_KEY` | only if you switch transcription to the Whisper API | |

`.env` is git-ignored. Keys are scrubbed from every log line.

### config.yaml

The defaults work. Things you will likely change:

- `paths.inbox`: the folder OBS records into (default `C:\Users\<you>\Streams\inbox`).
  In OBS: **Settings → Output → Recording → Recording Path**, point it at this folder.
  MKV is the safest OBS format (a crash doesn't corrupt it); the clipper reads MKV directly.
- `publishing.upload_post.user`: the Upload-Post profile name your accounts are connected to.
- `publishing.upload_post.timezone`: your time zone (posting slots are in this zone).
- `publishing.slots`, `max_posts_per_day`, `min_gap_minutes`: how posts are spread out.
- `brand`: font, colours, CTA text. Put your logo at `brand\logo.png` to get a watermark
  (your own logo only). Put custom `.ttf` fonts in `brand\fonts\` and set `brand.font` to the font's name.
- `transcription.model`: `large-v3` on an NVIDIA GPU; use `medium` or `small` on CPU-only machines.

Machine-specific overrides can go in `config.local.yaml` (same keys, git-ignored).

Run `.venv\Scripts\kir doctor` any time to check the setup.

### Start it automatically

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
```

This registers a Task Scheduler task called **KIR Clipper** that starts at login, runs hidden in the
background, and restarts itself if it crashes. Remove it with `scripts\uninstall-service.ps1`.
To run it in a window instead (handy the first time): `.venv\Scripts\kir run`.

### Connect your accounts (Upload-Post)

1. Create an Upload-Post account and a profile (e.g. `kir`), and put that name in `config.yaml`.
2. Connect TikTok, Instagram, and YouTube to that profile through Upload-Post's own login screens
   (official OAuth; the clipper never handles your passwords).
3. Instagram must be a **Business or Creator** account. TikTok posting needs a paid Upload-Post plan
   (about $24/month as of Oct 2026). Nothing here buys a plan for you.
4. Test with a private or low-stakes account first (section 4).

---

## 2. How it works day to day

Finish the stream. That's it. Roughly:

- **Stream ends** → the file sits unchanged for 60 seconds → it is moved to `Streams\processed\`
  (originals are never deleted; failures go to `Streams\failed\`).
- **Transcription**: on a decent NVIDIA GPU, a 1-hour stream takes a few minutes; on CPU it can take
  close to real time with `medium`.
- **Clip finding**: Claude reads the transcript and proposes moments, then re-reads each finalist to fix
  where it starts and ends, rescore it, and write titles, captions, and hashtags for each platform.
- **Rendering**: each clip is cut, reframed to vertical (following your face when it finds one), given
  word-by-word captions, a hook line for the first 2 seconds, a CTA at the end, and your watermark.
- **Score gate**:
  - **90+** → scheduled automatically on every platform in `publishing.platforms`
  - **60-89** → waits in **Needs review** on the dashboard
  - **below 60** → dropped and logged (visible under *Dropped / rejected*)
- **Posting**: clips are spread across your posting slots, best clip first, never more than the daily
  cap per platform and never closer than `min_gap_minutes`. TikTok posts are handed over at most 10 days
  ahead (TikTok's limit); other platforms can be scheduled further out.
- **Daily summary** (9am by default): what posted, what's coming up, how many clips are waiting for you.
- **Failures** retry after 5, 20, then 60 minutes, then you get an alert.

### The dashboard (http://127.0.0.1:8765)

- **Needs review**: watch the clip, edit the title/caption/hashtags or per-platform copy, then
  **Approve & schedule** or **Reject**. Under *Re-render* you can change caption style, crop, zoom, hook
  text, or nudge the start/end time and re-render.
- **Would auto-post**: during dry-run, the clips that *would* have gone out automatically.
- **Scheduled / Posted**: what's queued and what's live. On posted clips you can mark *Never repost* or
  *Always repost*.
- **Post calendar**: every planned post. Planned posts can be cancelled here; once handed to Upload-Post,
  cancel them in Upload-Post.
- **Stats**: the score distribution, score-vs-views once you import results, and recent streams.
- **Activity log**: every action the system took.

---

## 3. Dry run first (on by default)

`review.dry_run: true` in `config.yaml` means everything runs (transcribe, score, render, plan) but
**nothing is posted**. Do this for your first one or two streams:

1. Stream as normal. When the "Stream processed" notification arrives, open the dashboard.
2. Look at **Stats → Score distribution** and **Would auto-post**. Ask: is the number of 90+ clips per
   stream sensible (say 1-4)? Are those genuinely your best moments? Watch a few in *Needs review*.
3. If almost nothing reaches 90, or almost everything does, either edit `virality_criteria.md` (the
   plain-English brief Claude scores against) or change `review.auto_post_threshold`.
   You can re-score a stream for cheap: the transcript is cached, so only the Claude step re-runs.
4. When you're happy, set `review.dry_run: false` and restart the task (or reboot).

Tip: `review.render_rejected: true` renders the sub-60 clips too, so you can check what is being thrown away.

---

## 4. Going live safely

1. Connect a private/test account in Upload-Post first and point `publishing.upload_post.user` at it.
2. Set `dry_run: false`, approve one clip in the dashboard, and check it appears at the scheduled time
   with the right caption. `kir publish` runs one posting pass immediately.
3. Switch the profile to your real accounts.

The Upload-Post endpoint and field names (`/api/upload`, `platform[]`, `scheduled_date`, `timezone`,
`/api/uploadposts/status`) follow Upload-Post's published examples. If they change their API, the
adapter is one file: `kir_clipper/publishers/upload_post.py`. To switch services entirely (e.g. Postiz),
add another class with the same two methods (`schedule`, `status`) next to it.

---

## 5. The 90-day re-edit

Every day the clipper looks for clips whose most recent post went out 90+ days ago and makes a new version:

- different caption style, crop, zoom pattern, caption position, and watermark corner (at least 3 of
  these 5 change, compared with *every* earlier version),
- a new hook line, title, captions, and hashtags written by Claude, told what was used before,
- rendered fresh from the original recording (which is why originals are kept),
- then sent through the same 90/60 score gate and scheduling rules.

Capped at `reedit.max_reposts_per_week` (default 4) so old content doesn't crowd out new clips.
Once you have performance data for 5+ clips, clips that did worse than the median first time round are
skipped (unless you mark them *Always repost*).

Test it without waiting 90 days: `kir reedit --interval-days 1`.

---

## 6. Tuning with real results

Upload-Post's analytics access varies by plan, so performance import is a CSV for now:

```
post_id,views,likes,comments,shares,watch_time
12,15400,820,44,61,
```

(or use `platform,platform_post_id` instead of `post_id`). Then:

```powershell
kir perf-import results.csv
kir perf-report
```

The report shows median views per score band and how well the score predicts views. If the
correlation is near zero, the score isn't predicting performance yet: adjust `virality_criteria.md`.
If higher bands clearly do better, move `auto_post_threshold` to where the drop-off is.

The score measures structural quality (hook, payoff, length, topic). It can't see channel momentum or
trends, so treat it as a filter, not a guarantee.

---

## 7. Commands

All from the repo folder (`.venv\Scripts\kir ...`, or `python -m kir_clipper ...`):

| Command | What it does |
|---|---|
| `kir run` | Everything: watcher, posting loop, daily jobs, dashboard (what the service runs) |
| `kir doctor` | Check ffmpeg, keys, GPU encoder, folders |
| `kir process <file>` | Process one recording right now |
| `kir reprocess <stream_id>` | Re-run a failed stream (transcript is reused) |
| `kir serve` | Dashboard only |
| `kir publish` | One posting pass now |
| `kir reedit [--interval-days N]` | Run the re-edit job now |
| `kir summary [--send]` | Print (and optionally send) the daily summary |
| `kir perf-import <csv>` / `kir perf-report` | Performance data |

---

## 8. Where things live

```
Streams\inbox\          <- OBS records here
Streams\processed\      <- originals after processing (never deleted)
Streams\processed\duplicates\  <- the same recording dropped twice
Streams\failed\         <- recordings that errored (see the alert / dashboard)
Streams\kir-data\
    kir_clipper.db      <- SQLite: streams, clips, posts, performance, activity log
    transcripts\        <- cached transcripts (re-runs are free)
    clips\stream_0001\  <- rendered clips + a .recipe.json describing each edit
    logs\kir_clipper.log
    notifications.log   <- every summary and alert, even without a webhook
```

---

## 9. Costs

- **Claude** (`claude-opus-5-5` at medium effort by default): an estimate, not yet measured on a real
  stream, is about $1-2 per stream regardless of length up to the clip cap (most of the spend is the
  second-pass review of each finalist), so roughly $10-20/month at 8-9 streams. This is higher than the
  build guide's "few cents" figure. Ways to lower it: set `llm.model: claude-sonnet-5-5` (about half the
  price), `llm.effort: low`, or a smaller `highlights.max_clips_per_stream`. Check real spend in the
  Anthropic console after the first stream.
- **Transcription**: free with local faster-whisper (default). The Whisper API option is about $0.36/hour.
- **Upload-Post**: about $24/month for 5 profiles; TikTok requires a paid plan.
- Rendering uses your GPU (NVENC) when available, otherwise the CPU.

---

## 10. Troubleshooting

- **Nothing happens after a stream**: check `logs\kir_clipper.log`. Is OBS recording into `paths.inbox`?
  Files must have stopped changing for 60 seconds.
- **"faster-whisper ... cublas" errors**: re-run `scripts\install.ps1`, or set `transcription.device: cpu`.
- **Captions in the wrong font**: the font must be installed in Windows or placed in `brand\fonts\`, and
  `brand.font` must match the font's family name.
- **A stream failed**: you get an alert with the reason; the recording is in `Streams\failed\`.
  Fix the cause and run `kir reprocess <id>`.
- **Posting blocked**: `kir doctor` will say if `UPLOAD_POST_API_KEY` is missing. Rejected credentials
  stop retries immediately and alert you.

---

## For developers

```
kir_clipper/
  watcher.py      finished-file detection (size + mtime stable, not locked), inbox rescans
  pipeline.py     claim → probe → transcribe → highlights → gate → render → plan posts
  transcribe.py   chunked faster-whisper / Whisper API, seam-safe stitching, loudness track
  highlights.py   pass 1 candidates → dedupe → pass 2 boundary/copy refinement → snap → cap
  llm.py          Claude structured-output calls (with server-side refusal fallback)
  render.py       ffmpeg reframe (face/center/blur-pad), zoom, watermark; recipes
  captions.py     ASS word-by-word caption styles (bold_pop, karaoke, boxed, minimal)
  scheduler.py    slot planning, caps, spacing, submit/poll/retry/alert
  publishers/     Publisher interface, Upload-Post adapter, dry-run adapter
  reedit.py       90-day lineage selection, recipe variation, copy rewrite
  performance.py  CSV import and score-vs-views report
  web/            FastAPI + single-page dashboard (localhost only)
```

`pip install -e ".[dev]"` then `pytest`. The tests use a synthetic recording plus fake Whisper/Claude
and cover the acceptance tests in the build guide: drop-to-queue, half-written files, duplicates,
retry-then-alert, re-edit producing a visibly different file, and no secrets in logs. A 3-hour run was
checked separately (27k words stitched without gaps across chunks, ~55 MB peak memory excluding the
Whisper model, clip cap respected).

**Design note: captions are rendered with ffmpeg/libass instead of Remotion.** The build guide suggested
Remotion; libass gives the same word-by-word styles without adding a Node.js + headless-Chrome render
step on Windows, keeps everything in one ffmpeg pass (with NVENC), and has no licence considerations.
Because every render is described by a recipe, a Remotion renderer can be added later as another
renderer without touching the rest of the pipeline.
