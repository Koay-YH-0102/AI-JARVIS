# J.A.R.V.I.S. — Just A Really Virtuous Intelligent System

*(named that on purpose, so nobody sues anybody)*

A local web console that lets an LLM control your Windows computer — opening
apps, checking system info, changing settings, managing files — through a
fixed set of tools, plus voice input/output, file uploads, a task list, and a
searchable chat history. Runs entirely on your machine at
`http://127.0.0.1:5678` and is **not** reachable from your network.

## Tabs

- **Chat** — talk to J.A.R.V.I.S., by typing or by voice.
- **Tasks** — a to-do list of things you want it to do. Add one, hit **Run**
  to send it to Chat immediately, or just check it off by hand.
- **Files** — everything you've uploaded, with type/size/when.
- **Notifications** — risky actions land here for approval (see below).
- **History** — every past chat, auto-saved, click to reopen.
- **Core** — a full-width "now talking" view: just the reactive circle and a
  caption of what J.A.R.V.I.S. is saying, sidebar hidden for a clean look.

Settings (⚙) and **+ New chat** live as icon buttons in the top bar since
they're actions, not content to browse.

## 1. Install requirements

You need **Python 3.10+** on Windows. Then, in a terminal, from this folder:

```powershell
pip install -r requirements.txt
```

`pycaw`/`comtypes` (volume control) and `screen_brightness_control`
(brightness) are Windows-only. If either fails to install, delete it from
`requirements.txt` and re-run — the rest of the app works fine without them,
those two features just report "not installed."

## 2. Set up a provider

Pick one in Settings (you can switch anytime):

**OpenRouter** — create a key at https://openrouter.ai/keys, then paste it
and a model id from https://openrouter.ai/models (check the model supports
tool/function calling).

**Ollama (local, free)** — install from https://ollama.com, then:
```powershell
ollama pull llama3.1
```
(or another tool-calling-capable model). Leave Ollama running, then in
Settings pick the Ollama tab, confirm the base URL is
`http://localhost:11434`, and click **Fetch installed** to pick your model.
No API key needed.

**Google AI Studio** — create a key at https://aistudio.google.com/apikey,
then paste it and a model id such as `gemini-2.5-flash`.

## 3. Run it

```powershell
python app.py
```

Open **http://127.0.0.1:5678**.

## 4. Voice — talking to J.A.R.V.I.S. and having it talk back

**Voice input (🎤) runs on local Whisper** (`faster-whisper`) — click the mic,
speak, click again (or just pause — it auto-stops after about 1.4 seconds of
silence, or 15 seconds max). Your audio is recorded in the browser, sent once
to your own Flask server, transcribed **entirely on your machine**, and the
temp audio file is deleted immediately after. No cloud speech service is
involved — that's a real privacy improvement over the browser's built-in
speech recognition, which sends audio to Google/Microsoft's servers.

The tradeoff: it needs `faster-whisper` installed (`pip install
faster-whisper`, already in `requirements.txt`) and downloads a model from
Hugging Face the first time you use it — that first transcription will be
slow while it downloads, everything after is instant-ish. Pick a model size
in Settings → Voice:

| Model | Speed on CPU | Accuracy |
|---|---|---|
| `tiny` | fastest | roughest |
| `base` | good default | decent |
| `small` | slower | noticeably better |
| `medium` / `large-v3` | slow without a GPU | best |

If you have an NVIDIA GPU with CUDA + cuDNN set up, you can get much faster
transcription by changing `device="cpu"` to `device="cuda"` in
`get_whisper_model()` in `app.py` — that's a manual code edit, not a UI
toggle, since GPU setup varies too much to automate here.

**Clap to activate:** in Settings → Voice, check "Listen for a double-clap to
start voice input." This uses the Web Audio API to watch your mic for two
sharp volume spikes in under a second — that check happens entirely in your
browser and nothing is sent anywhere for it (separate from, and always free
regardless of, the Whisper transcription above). It's a simple
amplitude-spike detector, not real clap recognition, so a loud enough
non-clap sound (a book dropping, a door slam) can trigger it too, and very
muffled claps may not. Turn it off if it's too twitchy or not sensitive
enough for your mic; there's no tuning UI for the threshold yet, it's a fixed
heuristic in `app.js` (search for `peak > 80`).

**Voice replies:** in Settings → Voice, check "Speak replies aloud" to have
J.A.R.V.I.S. read its responses using your browser's speech synthesis
(`speechSynthesis` — free, built in, no API key, no server round-trip). The
core visualization pulses outward while it's speaking and shifts to a red
ring while it's listening to you — even with voice replies turned off, the
core still does a timed pulse and the caption still updates, so the **Core**
tab (below) stays useful either way.

## 5. Core tab — the "now talking" view

A dedicated tab that's just the reactive circle and a caption of what
J.A.R.V.I.S. is saying — nothing else, sidebar included, hides while you're
on it for a clean full-width view. It updates on every reply, not just ones
read aloud: the circle still pulses (using an estimated speaking duration)
and the caption still appears even with "Speak replies aloud" off in
Settings, so it's a useful glanceable status view regardless of whether audio
is on.

## 6. Files — upload almost anything

Attach files from the 📎 button in Chat, or upload standalone from the
**Files** tab. What actually happens with each type:

| Type | What J.A.R.V.I.S. sees |
|---|---|
| `.txt .md .csv .json .py .js .html .css .log .yml .xml` etc. | Full text, inlined into your message (truncated past ~8,000 characters) |
| `.pdf` | Extracted text, same truncation |
| `.docx` | Extracted text, same truncation |
| `.doc` (legacy binary Word) | **Not supported** — save as `.docx` and re-upload |
| `.png .jpg .jpeg .gif .webp .bmp` | Inlined as an image (under 6MB) for **vision-capable models only** — if your chosen model doesn't support images you'll get an error back; switch models or remove the image |
| `.mp3 .mp4 .wav .mov` etc. | **Stored on disk, not transcribed or watched.** J.A.R.V.I.S. is told the file's path and can act on it via tools (e.g. you could ask it to move or rename the file), but it can't hear or see the contents. This is separate from the 🎤 mic, which *does* use local Whisper (see the Voice section) — that pipeline only runs on live mic recordings, not on uploaded audio/video files, so an uploaded `.mp3` doesn't get auto-transcribed the way speaking into the mic does |
| anything else | Stored, path shared, contents not read |

## 7. Composer

Enter sends your message. **Shift+Enter** inserts a new line instead — handy
for multi-line instructions or pasted text.

## Notifications

Risky actions never run silently. When the assistant wants to do one, it
switches you to the Notifications tab automatically with a card showing the
exact action and arguments, and **Approve**/**Deny** buttons. Nothing happens
on your machine until you click Approve. Resolved items stay in the list
(grayed out) so you can see what you approved or denied recently.

While a confirmation is pending, sending a new chat message is blocked with a
reminder to resolve it first.

## Chat history

Every conversation auto-saves to `chats/<conversation_id>.json` after each
turn — full message history, including tool calls, results, and attachment
notes, in plain JSON. The History tab lists them by title and last-updated
time; click one to reopen it. Chats survive an app restart too.

## What it can and can't do

**Can:** launch/close apps, open Windows Settings pages, read system/battery
info, list running processes, take screenshots, set volume/brightness/mute,
lock the PC, search files, read a file's contents, list a folder's contents,
create folders, shut down/restart/sleep the PC (with confirmation), delete a
single file (with confirmation), empty the recycle bin (with confirmation),
run an arbitrary PowerShell command as a last resort (always requires
confirmation), take voice input, speak replies, read/see uploaded
text/PDF/Word/image files.

**Won't do:** uninstall software programmatically (opens Apps & Features for
you to do it yourself), delete whole folders, transcribe *uploaded*
audio/video files (live mic input is different — see Voice), or do anything
destructive without a Notifications confirmation first.

## Notes on each provider

- Not every model supports tool/function calling. If the assistant only ever
  replies in plain text and never actually does anything, try a different
  model.
- **Ollama** performance and tool-calling reliability depend heavily on the
  model and your hardware. Smaller local models are more likely to call the
  wrong tool or hallucinate arguments than large hosted models — the
  confirmation gate matters more here, not less.
- Switching providers mid-conversation isn't supported — each browser tab
  keeps one running conversation tied to whichever provider was active when
  it started. Refresh the page (or change providers before your first
  message) to start clean on a new one.

## Volume control on Python 3.14

`requirements.txt` pins `comtypes>=1.4.16` — earlier releases crash on
Python 3.14 with a `NameError` during import, silently disabling volume
control. If `pip install` gave you an older comtypes before this fix, run
`pip install --upgrade comtypes` and restart the app.

## Extending it

Add new abilities in `tools.py`:
1. Write the Python function.
2. Add its name to either `SAFE_ACTIONS` or `RISKY_ACTIONS`.
3. Add its dispatch entry to `DISPATCH`.
4. Add a matching schema entry to `TOOL_SPECS` so the model knows it exists.

If you skip step 2, the tool is treated as risky by default (fail-closed).
Steps 3 and 4 are checked automatically too — two `assert` statements at the
bottom of `tools.py` compare `DISPATCH` against `SAFE_ACTIONS | RISKY_ACTIONS`
and against `TOOL_SPECS`, and the app refuses to start if any tool is missing
from one of the four places, with an error telling you exactly which list.

## Security notes

- The server binds to `127.0.0.1` only — not your local network.
- Your API keys live in `config.json` on disk, in plain text. Don't commit
  that file or share the folder without removing it first.
- `run_shell_command` gives the model a genuine escape hatch to run PowerShell
  — it is always gated behind a Notifications confirmation, but you're
  trusting whatever model you pick to propose reasonable commands. Read the
  command in the notification card before approving it.
- `read_file` is classified as safe (it can't modify anything), but it does
  let the model read any file it's given a path to and send the contents to
  whichever provider you've configured.
- Chat transcripts in `chats/` and files in `uploads/` include full message
  and attachment content — same sensitivity as `config.json`, don't share
  those folders carelessly.
- Voice input now runs entirely on your machine via local Whisper — no audio
  leaves your computer for transcription (a change from a browser-based
  speech API, which would send audio to a cloud service). Voice replies
  (text-to-speech) and clap detection also never leave your browser.
  Transcribed audio is written to a temp file in `uploads/` for the duration
  of one request and deleted immediately after — it's not kept.
- Ollama runs unauthenticated on localhost by default — fine for normal
  single-user use, but don't reconfigure it to listen on your network without
  adding your own auth in front of it.
