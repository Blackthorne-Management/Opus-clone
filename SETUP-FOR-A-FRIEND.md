# Setting up your own Auto-Clipper

This turns your stream recordings into captioned vertical clips (TikTok, Reels, Shorts), scores them, and
can post them on a schedule. It also edits any video you drop in, finds what's working in your niche,
and writes scripts in your voice. It runs on your own Windows PC, under your own accounts and keys.

**About 20 minutes the first time.**

## What you need

- A **Windows 10 or 11** PC. An NVIDIA graphics card makes it much faster, but isn't required.
- **OBS** (or any recorder that saves files to a folder).
- A **Claude API key**: [console.anthropic.com](https://console.anthropic.com) → sign up → add a payment
  method → **API keys** → **Create key**. Roughly $1-2 per stream processed.
- Optional, later:
  - **Upload-Post** account, for automatic posting (about $24/month).
  - **Gemini key**, for AI thumbnail art ([aistudio.google.com](https://aistudio.google.com)).

## 1. Download

1. Open **https://github.com/Blackthorne-Management/Opus-clone/releases/latest**
2. Under **Assets**, click **Auto-Clipper-<version>.zip**.
3. Open your Downloads folder, right-click the zip → **Extract All...** and choose somewhere permanent,
   such as `Documents`. You get a folder called **Auto-Clipper**.

## 2. Install

1. Open the **Auto-Clipper** folder and double-click **Install.bat**.
2. If Windows shows "Windows protected your PC", click **More info** → **Run anyway**.
3. It asks to install **Python** and **FFmpeg** if you don't have them. Answer **Y**.
   - If it then says to close the window and run it again, do that.
4. It installs the app. The first time takes a few minutes.
5. Paste your **Claude API key** when it asks, then press Enter.
6. Answer **Y** to "Start the clipper now, and automatically every time you log in?".
7. Your browser opens the dashboard at **http://127.0.0.1:8765**.

## 3. Make it yours (in the dashboard)

The dashboard opens on **Settings**. Fill in **Make it yours** at the top:

- **Your creator name** and **time zone** (the time zone is usually filled in already).
- **Call to action** shown at the end of clips, e.g. "Follow @you for more".
- **Folder OBS records into**. Keep the one shown unless you already record somewhere else.
- **Your logo** (optional, PNG).
- **What you stream about** and **what your viewers come for**. Claude uses these to decide which
  moments make good clips. You can rewrite the full description any time in the same place.

Click **Save my details**. If you changed the recordings folder, restart the clipper:
log out of Windows and back in, or double-click **Start.bat**.

## 4. Point OBS at the folder

OBS → **Settings** → **Output** → **Recording**:
- **Recording Path**: the folder from step 3.
- **Recording Format**: **mkv** (the safest; a crash doesn't ruin the file).

## 5. First stream (nothing posts yet)

The app starts in **dry run**: it finds, scores and renders clips, but posts nothing.

1. Stream and record as usual. When you stop recording, the app waits a minute, then starts working.
2. A 1-hour stream takes a few minutes with an NVIDIA card, or longer without one.
3. Open the dashboard → **Needs review** and **Would auto-post**. Watch the clips.
4. Too many or too few clips scoring 90+? Change "What makes a good clip" in **Make it yours**, or the
   scores in **Settings**.

Other tabs to try:
- **Edit a video**: drop any phone video and get a finished vertical edit.
- **Niche research**: find which posts are beating other creators' usual numbers.
- **Script studio**: write a script for a new video. Click **Build my voice from my clips** after your
  first stream.

## 6. Posting for real (when you're ready)

1. Make an **Upload-Post** account and a profile.
2. Connect your TikTok, Instagram and YouTube to that profile, inside Upload-Post.
3. Copy your Upload-Post API key into the **.env** file in the Auto-Clipper folder. Open it with
   Notepad, put the key after `UPLOAD_POST_API_KEY=`, and save.
4. In **Make it yours**, enter the Upload-Post profile name.
5. **Settings** → change **Dry run** to **Live** → **Save changes**.

The full guide is in **README.md** in the folder, sections 4 and 5.

## Updating

1. Download the newest zip from the same Releases page.
2. Extract it over your old **Auto-Clipper** folder and choose **Replace** when asked. Your settings,
   keys, clips and personal files aren't in the zip, so they're kept.
3. Double-click **Install.bat** again.

## If something goes wrong

- **Dashboard won't open**: double-click **Start.bat**. It runs the clipper in a window and shows any
  error.
- **Nothing happens after a stream**: check that OBS saves into the folder from step 3, and that the
  file has finished saving.
- **Check everything at once**: in the Auto-Clipper folder, click the address bar, type `cmd`, press
  Enter, then run `.venv\Scripts\python -m kir_clipper doctor`. It lists what's missing.
- **Stop it starting at login**: in the `scripts` folder, right-click **uninstall-service.ps1** →
  **Run with PowerShell**.
