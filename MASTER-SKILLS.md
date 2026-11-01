# What came from the master-skills repo

The [master-skills](https://github.com/Jakeschincariol/master-skills) pack (MIT, by Jake Schincariol) has three
Claude skills: **/edit**, **/virality** and **/build**. This app folds in everything from /edit and /virality
that an auto-clipper can use. Where the skill did something better than the clipper, the skill's way replaced
the clipper's. The licence is in `kir_clipper/virality/LICENSE-master-skills.txt`.

**Kept** = in the app and works the same way. **Adapted** = in the app, reworked to run automatically.
**Replaced** = the clipper's old way was swapped for the skill's. **Left out** = not in the app, with the reason.

## /edit (video editing)

| Skill feature | In the app | Where |
|---|---|---|
| Cut dead space: adaptive speech gate, breath pads, frame-accurate joins, crossfades | Kept | Every clip, on by default; **Edit a video** tab |
| Cut "um"/"uh" from the transcript | Kept | Same |
| Retake detection (last take wins, exact and fuzzy) | Kept | Same |
| Pace presets: tight / normal / loose | Kept | Re-render panel on each clip; `jumpcut:` in config.yaml |
| Review the cut list before rendering and veto cuts | Adapted | Re-render → **Jump cuts** list: untick a cut to keep that moment |
| Never cut through a spoken word | Kept | Same |
| Audio: one fixed gain to -14 LUFS + true-peak limiter, no compression | **Replaced** the clipper's single-pass loudnorm on short clips | Every clip; long-form already used measured two-pass levelling and is unchanged |
| -16 LUFS fallback when peaks would be squashed | Kept | Shown on the clip card |
| Stop motion from a live clip (8/10/12 drawings per second, seeded wobble) | Kept | Re-render → Stop motion; Edit a video |
| Stop motion from a folder of photos (hold, loop, ping-pong, fit) | Kept | Edit a video → drop photos |
| Stop-motion styles (clay, paper cut-out, pixel...) | Adapted | Four looks as filters: classic, clay, poster (paper cut-out), pixel |
| Word-timed 1-3 word captions | Kept (the clipper's own 5 caption styles are better and stay) | Every clip |
| Caption file you can upload (.srt) | Kept | **Captions .srt** button on each clip card |
| Edit any raw take, not just streams | Adapted | **Edit a video** tab, `kir edit` |
| Probe / setup doctor | Kept | `kir doctor` (now also checks yt-dlp and the limiter) |
| Export spec (1080x1920, H.264, AAC 48 kHz, faststart) and self-check | Kept | Every render; sync and loudness are recorded per clip |
| Generating stop-motion frames with an image AI (model sheet, frame by frame) | **Left out** | Needs an image AI driven by hand frame by frame. Draw or shoot the photos, then use Edit a video |
| Motion graphics and b-roll overlays through HyperFrames, Remotion or video-use | **Left out** | These are separate apps (Node.js, a headless browser, an ElevenLabs key) that Claude drives by hand while you watch. Long-form videos already get chapter cards, lower thirds, pop-ups and zooms, and shorts get punch-in zooms |
| "Shot length under 1.5 s" pacing rule | **Left out** | Livestream clips are one camera, so there is nothing to cut between; the jump cuts do the tightening |

## /virality (finding what works and writing for it)

| Skill feature | In the app | Where |
|---|---|---|
| Collect posts from a paste, CSV or yt-dlp JSON | Kept | **Niche research** tab, `kir research add` |
| Public YouTube Shorts with view counts, no login | Kept | Niche research → Fetch Shorts (needs yt-dlp, installed by Install.bat) |
| Rank by views as a multiple of each creator's own median | Kept | Niche research |
| Your own posts as the best evidence | Adapted | Your posted clips with results are added automatically |
| Name the hook formula (26-formula library) | Kept | Research, every clip card, Script studio |
| Break the winners down: format, structure, CTA, pace, "the one thing they share" | Kept | Research → **Break the winners down**; feeds Write my version |
| Pull a winner's transcript | Kept (YouTube, through yt-dlp) | Used by the breakdown |
| Hook score (5 checks) + payoff rule (money, time or a number in the first 8 words) | Kept | Every clip card (hooks A and B), Script studio |
| Strip AI tells (invisible characters, em dashes, slop words) | Kept, with bugs fixed | All Claude-written titles, captions, hooks, YouTube metadata, scripts |
| Flag structural tells ("it's not just X, it's Y") | Kept, patterns widened | Clip cards; the studio rewrites them once automatically |
| "Reads as human" panel | Kept | Script studio |
| Voice profile from your own samples, pace measured | Adapted | Script studio → Build my voice from my clips (reads your clip transcripts) |
| Audience file, offer, comment keyword | Kept | Script studio → Your audience |
| Five hooks on five formulas, pick the best with a payoff | Kept | Script studio (it also prefers hooks with no blanks left to fill) |
| Shoot-ready script: HOOK / PROOF / STEP / REHOOK / PAYOFF / CTA, SAY / SCREEN / VISUAL, timed | Kept | Script studio, download as Markdown |
| One CTA: comment a keyword | Kept | Script studio |
| Caption, 3-5 hashtags, cover + title pair check | Kept | Script studio |
| LinkedIn version | Kept | Script studio |
| Never invent numbers ({{your number}}) | Kept | Script studio |
| "Shoot it" log, then ask for the views next time | Adapted | "I shot it" button; once the video is posted through the app its views come in automatically and show up in Niche research as your own results |

## /build (websites and apps)

**Left out entirely.** It's a step-by-step guide for building websites (spec, design, QA in a browser,
deploy), plus a site checker, colour-contrast checker and preview server. None of that applies to a video
clipper. It's still useful for your websites, used as a Claude skill on its own.

## Fixed along the way (bugs found in the skill)

- The AI-tell cleaner left double full stops ("a big deal..") and lowercase sentence starts after deleting a
  phrase, and missed the most common patterns ("isn't just X, it's Y", "It's not about X, it's about Y",
  "unlocked", "unlock your full potential"). Fixed and covered by tests.
- Some tools pointed to a `finish.py` audio script that didn't exist. The app has a working one (`audio.py`).
- Two tools gave the same hook different formula names. The app uses one classifier everywhere.
