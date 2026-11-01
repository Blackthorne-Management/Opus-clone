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

It also edits any video you drop in, finds what's working in your niche, and writes scripts in your voice.
Those parts came from the master-skills repo; **[MASTER-SKILLS.md](MASTER-SKILLS.md)** lists what was kept,
what was adapted, and what was left out.

**Setting it up for someone else?** Send them **[SETUP-FOR-A-FRIEND.md](SETUP-FOR-A-FRIEND.md)**. They
download the zip from the Releases page, double-click `Install.bat`, and fill in **Settings → Make it yours**.
Their name, keys and preferences stay on their PC.

---

## 1. Setup (once)

**Easiest:** double-click **`Install.bat`** in the app folder. It installs Python and FFmpeg with winget if
they're missing, installs the app, asks for your Claude key, and can start it at login. Then fill in
**Settings → Make it yours** in the dashboard. Run it again after every update.

By hand (needs **Python 3.11+**, **ffmpeg**, and **git** on Windows):

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
| `GEMINI_API_KEY` | aistudio.google.com → Get API key (optional) | AI thumbnail art; without it thumbnails use the template |
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
3. If almost nothing reaches 90, or almost everything does, either edit the clip criteria in **Settings → Make it
   yours** (saved as `my-criteria.md`; before setup it's `virality_criteria.md`) (the
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

## 5b. Long-form YouTube version of every stream

Besides the clips, each stream also becomes a full-length, lightly edited YouTube video.

**The edit stays mostly intact.** Only moments where you aren't actually delivering content are removed:

| Cut | Example |
|---|---|
| distracted | looking at something else, side conversation |
| interrupted | cut off by the door/phone/someone walking in, restarting a sentence |
| chat_reading | reading usernames, comments, shout-outs aloud. If a chat question leads into a real answer, only the reading-out is cut; your answer stays |
| tech_issue | "can you hear me", fixing OBS or the mic |
| away | AFK, "be right back" |
| housekeeping | "starting soon", setup, ad reads |

Claude only cuts what it is at least 70% sure about (`min_cut_confidence`), and never more than 25% of a
video (`max_cut_ratio`). Pauses longer than 3 seconds are shortened to 0.8 seconds. Remove a category
from `longform.cut_categories` in `config.yaml` if you never want it cut (for example, delete
`chat_reading` if you'd rather keep chat interaction in).

**What gets added:** an animated intro (video title + your brand), a "CHAPTER N" title card at each new
topic, lower-third topic banners that slide in, pop-up key points, quick punch-in zooms on emphasis, a
"Subscribe" reminder a third of the way in, your logo watermark, and an outro. YouTube chapters, title,
description and tags are written for you.

To use your own designed intro/outro instead of the generated ones, put them at `brand\intro.mp4` and
`brand\outro.mp4`. Set `longform.outro_lines` to change the outro text.

**How it works day to day:**

1. After a stream is processed, the dashboard's **Long-form** tab shows it with suggested segments
   (a silence of 3+ minutes is pre-filled as a split point).
2. Keep it as one video, or click **+ Split** and set the start/end times (h:mm:ss) for each segment.
   Each segment becomes its own YouTube video. Click **Build**.
3. Claude plans the edit and the video renders in the background. On CPU only, rendering took about
   two-thirds of the video's length in testing (so roughly 80 minutes for a 2-hour stream); NVENC on an
   NVIDIA GPU should be noticeably faster, but that hasn't been measured yet.
4. Review it: watch the video, edit the title/description/tags, and look at the **Cuts** list. Untick
   any cut you want to keep and click **Re-render with changes** (Claude isn't asked again, so this is
   free). **Re-plan from scratch** asks Claude for a fresh edit.
5. **Approve & schedule on YouTube**. Long-form videos never auto-post; they always wait for you.

You can change how a stream is split later from the same tab, until one of its videos is scheduled.
From the command line: `kir longform list`, `kir longform whole <stream id>` (build the whole stream as one
video), `kir longform build <id>`.

Disk space: a 2-hour 1080p edit is a few GB, and rendering needs about the same again temporarily.
Upload-Post's limits on long YouTube uploads (file size, length) should be checked on your plan before
relying on it; the upload of a multi-GB file can take a while on home internet.

---

## 5c. Post everything five times

Every short clip that gets approved (automatically at 90+, or by you) follows this plan:

| # | Step | When | Where |
|---|---|---|---|
| 1 | Trial Reel | day 1 | Instagram, shown to non-followers first |
| 2 | Feed Reel | day 1, a later slot than the trial | Instagram, TikTok, YouTube |
| 3 | Mirrored Trial Reel | day 2 | Instagram. The picture is flipped left-to-right, captions stay readable |
| 4 | Feed Reel +90 days | ~90 days later | re-edited version (new look + new hook), all platforms |
| 5 | Feed Reel +180 days | ~180 days later | another re-edit, then it stops |

Every clip card in **Scheduled / Posted** shows its **Posting plan** with each step's status and a live
countdown ("in 1d 6h"), including the projected dates for steps 4 and 5. The **Post calendar** lists every
upcoming post with its step and countdown.

**Trial Reels are automatic.** They go out through Upload-Post with
`share_mode: TRIAL_REELS_SHARE_TO_FOLLOWERS_IF_LIKED`, so Instagram shows them to non-followers first and
shares them with your followers if they do well. Instagram only allows trial reels on public professional
accounts with 1,000+ followers. If Upload-Post ever rejects one, that post switches to "post by hand":
you get a reminder, then Download it, post it in Instagram as a Trial Reel, and tap **Mark posted**.
`cadence.trial_mode` can also be `manual` (always by hand) or `skip` (no trial reels).

Change the steps under `cadence.steps` in `config.yaml` (which platforms, which day). Steps 4 and 5 come
from the `reedit` section: `max_reposts: 2` stops after the second repost, and `style: same` reposts the
original file instead of a re-edit. Set `cadence.enabled: false` to go back to one post per platform.

### Reels you posted before using the app

Open **Import posted reel** in the dashboard to add reels you've already posted, one at a time:

1. Choose the video file (the exact reel you posted).
2. Enter **when it was first posted**. That's day 1 of its schedule, and the form shows each step's date.
3. Tick the platforms it went out on and the steps you've **already done** (Feed Reel is ticked by default).
4. Choose what happens to steps whose date has already passed: **skip them** or **post them as soon as possible**.
5. Tick **Post the +90/+180 day reposts automatically**, or untick it to review each repost first.

The app records what's done, schedules what's left, and the reel then shows up in **Posted** with the same
numbered plan and countdowns as everything else. Overdue +90/+180 reposts are picked up at the next daily run.

When you pick a reel, a pop-up asks whether it has captions or text on screen and explains exactly what to do.
If you upload a raw recording as the clean copy and it's a different length from the reel, the app asks
where the part you used starts and ends (for example 1:05 to 1:50).

**On-screen text.** Most finished reels have captions burned in. Flipping one for the Mirrored Trial Reel
would make the text read backwards, so that step is skipped unless you either untick "The reel has
captions or text on screen" or upload a **clean copy** without text. With a clean copy the app transcribes
it, makes a properly mirrored version with fresh captions, and makes real re-edits at +90/+180 days.
Without one, the reposts reuse the file you posted.

From the command line (handy for a batch):
`kir import my_reel.mp4 --posted "2026-09-20 18:00" --done feed,trial --platforms instagram,tiktok`
(`--clean clean.mp4`, `--no-text`, `--missed now`, `--review` are optional).

---

## 5d. Settings, phone access, and keeping it healthy

**Settings tab.** Change the important options without editing files: Dry run / Live, the auto-post and
drop scores, clips per stream, the five-times plan, posting times and daily caps, long-form on/off, and
phone access. Changes apply immediately and are saved to `config.local.yaml`. Going live asks you to
confirm, and refuses if the Upload-Post key isn't set yet.

**Phone access (recommended for Trial Reels).** Trial Reels have to be posted from the Instagram app, but
the files are on your PC. Turn on phone access in Settings, set a 4-8 digit PIN, and restart the app. Then
open the address shown in Settings (like `http://192.168.1.20:8765`) on your phone while on the same Wi-Fi
and enter the PIN. Windows may ask to allow the connection the first time: choose **Private networks**.
Reminders for trial reels include this link. From the phone: **Post calendar → Download → post in
Instagram → Mark posted**. Five wrong PINs lock that device out for 5 minutes. It only works on your
home network, not over the internet.

**Status bar.** Under the tabs: whether the background service is running, what's being processed,
long-form edits in progress, if posting is blocked (and why), and free disk space.

**Backups and disk space.** The database is backed up every day to `kir-data\backups` (last 14 kept).
If free space drops below 20 GB (`maintenance.min_free_gb`), the daily summary warns you. Recordings and
renders are large, so move old files out of `Streams\processed` now and then.

---

## 5e. YouTube thumbnails, results tracking, best times, and hook tests

**Thumbnails (long-form videos).** Designed from scratch, not grabbed from the video, and always in the
same channel style so your videos look like a set: a red/blue complementary background (split, spotlight
or versus), 1-4 huge words with a thick outline and one word in yellow, a cut-out photo of you with a
white outline, one accent graphic (arrow, ring, $, ✓/✗, ?, rising chart), and your logo. Claude writes three
concepts per video. In the Long-form tab, click the one you like or **Make 3 new options**.

- Put cut-out photos of yourself in `brand\thumbnail\person\`, named by expression (`shocked.png`,
  `serious.png`, `pointing.png`...). Transparent PNGs work best; on iPhone, touch and hold yourself in a
  photo and choose Copy/Share to get one. Without photos, thumbnails are text-only.
- **AI art from Google Gemini.** Add `GEMINI_API_KEY` to `.env` (get one at aistudio.google.com; billing
  must be on). In the default **background** mode Gemini paints a unique scene for each thumbnail and the app
  grades it into the red/blue look and adds the words, your photo and the accent, so every thumbnail is
  different but clearly part of the same channel. **Full** mode (Settings tab) lets Gemini design the whole
  thumbnail from your photos, with your recently chosen thumbnails as style examples so it stays on-brand.
  Cost is about $0.07 per image, so roughly $0.20 per video for three options (`thumbnails.ai` in
  `config.yaml`). If Gemini is unavailable, the template is used and the option shows why.
- Colours and font are under `thumbnails:` in `config.yaml` (Anton is bundled).
- Preview the style any time: `kir thumbnails --title "Why discipline beats motivation"`.
- Upload-Post can only attach a thumbnail from a public web link, so when a long-form video goes live you
  get a reminder to upload the chosen thumbnail in YouTube Studio (Download button in the Long-form tab).

**Results tracking.** Views, likes, comments, shares, saves, reach and watch time are fetched from
Upload-Post 1, 3, 7 and 30 days after each post (they show in the Post calendar). Posts you made by hand
get an **Add numbers** button.

**Best posting times.** The Stats tab shows, per platform, how your posts did by the hour they went out,
and once there are 20+ posts with results it suggests better times (keeping one slot testing a new hour so
it keeps learning). Click **Use these times**, or set `optimize.auto_apply: true` to update them every
Monday. Each platform can now have its own times.

**Hook A/B tests.** Every new clip gets two on-screen hooks: the Trial Reel uses hook A and the Mirrored
Trial Reel hook B. Both go to non-followers, so after 3 days the one with more views wins (within 15% is a
tie). Winners are shown in Stats, the +90/+180 reposts build on the winning angle, and Claude sees your
recent winners and losers when writing hooks for new clips.

---

## 5f. Editing, research and scripts (merged in from the master-skills repo)

These come from Jake Schincariol's free [master-skills](https://github.com/Jakeschincariol/master-skills)
pack (MIT): the `/edit` and `/virality` skills, rebuilt as parts of this app. Where the skill did something
better than the clipper did, the skill's way replaced it.

### Tighter clips (on by default)

- **Jump cuts inside every clip.** Dead air, "um"/"uh", and retakes (a line said twice in a row: the last take
  wins) are cut, each with a breath kept on both sides so no word is clipped. Joins get a 30 ms crossfade, and
  every cut lands on a whole video frame, so sound and picture stay in sync. If the clip has music or game audio
  under your voice, pauses are found from the transcript instead. A safety cap stops it gutting a clip:
  `jumpcut.max_cut_ratio` (40%). Change the pace per clip under **Re-render** (tight / normal / loose / off),
  or for everything under `jumpcut:` in `config.yaml`. Whisper doesn't always write down "um", so not every
  one is caught. The card shows what was cut ("Tightened 52s → 44s: 9 pauses, 2 ums").
- **Audio mastered, not auto-levelled.** *Replaced:* clips used to go through single-pass `loudnorm`, which
  rides the volume like an automatic gain control (it lifts room echo between words). Now each clip is
  measured, raised or lowered by one fixed amount to **-14 LUFS**, and only the peaks are caught by a limiter
  at -1.5 dBTP. If that would squash the peaks by more than 6 dB, it masters to -16 LUFS instead and the card
  says so. Long-form videos already used measured two-pass levelling and are unchanged.
- **Stop motion** as a look on any clip: Re-render → Stop motion (classic wobble, clay, poster, pixel; 8, 10 or
  12 drawings per second). Captions, the hook and your logo stay smooth on top.

### Hook check and AI-tell cleanup (every clip, free, no API calls)

- Every on-screen hook (A and B) is scored 0-100 on five checks, named against a library of 26 hook formulas,
  and checked for **Jake's payoff rule**: does it lead with money, time or a number in the first 8 words? It
  shows on each clip card. It is good at catching weak hooks (greetings, preambles, nothing concrete) and is
  **not** a view predictor; Claude's 0-99 score still decides auto-posting.
- Every title, caption, on-screen hook and per-platform caption Claude writes is cleaned: em dashes, invisible
  characters and stock phrases ("game changer", "let's dive in", "journey") are fixed automatically, and
  structural tells ("it's not just X, it's Y", "The result?") are flagged on the card for you to rewrite.
  Claude is also told up front not to write them. Your own spoken words and your hashtags are never touched.

### Edit a video (new tab)

Drop in **any** video (a phone take, a reel you recorded) and get a postable 9:16 edit: jump cuts, captions word
by word, the hook on screen, face-following framing, mastered audio, and optional stop motion. Claude can write
the hook, title, captions and hashtags, or you write them. Or drop in **photos** to make a stop-motion video (one
photo per drawing, optional sound or voice-over). The result waits in **Needs review** with the same Re-render
controls, then follows the same five-step posting plan once you approve it. Uploads never post on their own.
Command line: `kir edit take.mp4` (or `kir edit *.jpg --look clay`).

### Niche research (new tab)

Find what's already working. Paste posts (`creator | views | hook | url`), upload an analytics CSV, or fetch a
public YouTube channel's Shorts (needs `yt-dlp`: `.venv\Scripts\python -m pip install yt-dlp`). Every post is
ranked by its views **as a multiple of that creator's own median**, so a small account's breakout beats a big
account's normal day; creators need 4+ posts. Your own posted clips with results are included automatically.
The tab names the hook formula behind each outlier and says how much evidence you have (12 posts is an
anecdote; 30+ across 6+ creators is evidence). **Write my version** sends a winner to the Script studio.
Nothing logs in, uses cookies, or scrapes behind a login.

### Script studio (new tab, uses Claude)

Shoot-ready scripts for **new** videos, in your voice. Five hooks on five formulas; the best one that is
shootable (no blanks left) and leads with a payoff opens the script. Then timed beats (SAY / SCREEN / VISUAL),
one comment-a-keyword CTA, caption, hashtags, cover and title, plus a LinkedIn version. Everything is timed at
**your** pace (measured from your clips), cleaned of AI tells (one automatic rewrite if any are left), and never
invents a number: unknowns stay as `{{your number}}` for you to fill in. Fill in **Your voice** once: **Build my
voice from my clips** reads your best clips' transcripts and writes `brand\my-voice.md` with your real lines; add
the numbers you'll say on camera under "Proof I can say out loud". `brand\my-audience.md` describes your one viewer
and offer. Download or copy each script as Markdown. Each script costs one or two Claude calls.

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
correlation is near zero, the score isn't predicting performance yet: adjust the clip criteria (Settings → Make it yours).
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
| `kir longform list \| whole <stream> \| build <id>` | Long-form YouTube edits |
| `kir import <file> --posted "YYYY-MM-DD HH:MM" [--done feed,trial]` | Add a reel you already posted to the schedule |
| `kir thumbnails --title "..."` | Preview the thumbnail style for a title |
| `kir edit <video or photos> [--pace tight] [--look clay]` | Edit a raw take (or make stop motion); it waits in Needs review |
| `kir research add <file> \| fetch <@handle> \| report` | Niche research from the command line |
| `kir script "the idea"` | Write a shoot-ready script and print it |

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
    clips\stream_0001\  <- rendered clips + a .recipe.json describing each edit and .edit.json (cuts, audio)
    clips\upload_0001\  <- videos from the Edit a video tab
    edits\edit_0001\    <- the uploads themselves (kept, so re-renders and re-edits work)
    longform\stream_0001\ <- full-length YouTube edits
    logs\kir_clipper.log
    notifications.log   <- every summary and alert, even without a webhook
```

---

## 9. Costs

- **Script studio / voice builder / Edit a video copy**: one Claude call each (a script may take a second
  call to rewrite lines that still read as machine-written). Hook checks, AI-tell cleanup, jump cuts, audio
  mastering, stop motion and niche research are local and free.
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
  render.py       ffmpeg jump cuts, reframe (face/center/blur-pad), zoom, stop motion, watermark; recipes
  jumpcut.py      dead air / filler / retake cuts on the frame grid (from master-skills /edit)
  audio.py        fixed-gain -14 LUFS mastering + true-peak limiter (from master-skills /edit)
  stopmotion.py   stop-motion looks for video and photos (from master-skills /edit)
  editor.py       "Edit a video": uploads -> review clips
  research.py     niche research: outliers vs each creator's median (from master-skills /virality)
  studio.py       script studio: voice profile, scripts, hook pick, timing (from master-skills /virality)
  virality/       hook scorer + 26-formula library, AI-tell cleaner, detector, collector, speech timing
                  (copied from master-skills /virality, MIT, origin headers kept; humanize bugs fixed)
  captions.py     ASS word-by-word caption styles (hormozi [default], bold_pop, karaoke, boxed, minimal)
  scheduler.py    slot planning, caps, spacing, submit/poll/retry/alert
  publishers/     Publisher interface, Upload-Post adapter, dry-run adapter
  reedit.py       90-day lineage selection, recipe variation, copy rewrite
  longform.py     segments, Claude edit plan, frame-exact long-form render, review edits
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
