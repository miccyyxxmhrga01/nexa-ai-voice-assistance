# Nexa: an AI assistant you can talk to, online or offline

A voice-only assistant with a glowing 3D talking orb. Her brain, hearing
and voice all run on your own computer, so she works with no internet. When you
*are* online she can search the web, and think with Groq, Gemini or Claude if you add
a key. Say **"Hey Nexa"** and she answers out loud.

Made by **Mic Dahryl Caybin**.

| Part | What it uses | Runs on |
| --- | --- | --- |
| Brain (offline) | [Gemma 3](https://ai.google.dev/gemma) served by [Ollama](https://ollama.com) | this PC |
| Brain (online, optional) | Groq, Gemini or Claude, with your own API key | the internet |
| Hearing | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`base.en` and `small.en`) | this PC (NVIDIA card if there is one) |
| Voice | [Piper](https://github.com/OHF-voice/piper1-gpl) neural voices, male by default, and [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) casual voices | this PC |
| PC commands | Start menu apps, websites, music and volume keys | this PC |
| Web search | DuckDuckGo, wttr.in and Wikipedia. No key needed | the internet |
| App | Python (FastAPI) at `http://127.0.0.1:8000`, in its own Chrome/Edge window | this PC |

## Install on a Windows PC

You need Windows 10 or 11 (64-bit), a microphone, about 5 GB of free disk space,
8 GB of RAM or more, and internet for the first setup only.

1. Double-click **NexaSetup.exe**. The installer isn't code-signed yet, so Windows may stop it:
   - *"Windows protected your PC"* (SmartScreen): click **More info** → **Run anyway**.
   - *"Smart App Control blocked…"*: there's no exception for a single app. The only way
     through is to turn Smart App Control off in **Windows Security → App & browser control →
     Smart App Control settings**. On Windows 11 with the April 2026 update or later it can be
     turned back on afterwards (older versions need a Windows reset for that), but it may then
     block parts of Nexa again.
2. Press **Install**. No admin rights are needed; Nexa goes into your own Programs folder.

Setup downloads everything she needs. It takes 10 to 30 minutes depending on your
connection, and you can keep using your PC (press **Show details** to watch):
- Python, if it isn't installed
- Ollama and the Gemma model (about 0.8 GB)
- Nexa's hearing (about 0.6 GB) and five voices (about 370 MB)
- on PCs with an NVIDIA card: its speed-up for hearing, and six casual-sounding voices that run
  on it (about 2.5 GB in all)

When it's done, Nexa's window opens with just her animated orb. Tap it to start
voice setup and click **Allow** if your browser asks for the microphone. A **Nexa** shortcut is added to the Start menu (and the desktop, if you left
that ticked); use it from now on. No internet is needed after this.

If setup stops halfway (for example the Wi-Fi drops), run NexaSetup.exe again and press
**Try again**; it picks up where it stopped.

**Without the installer:** right-click **Nexa.zip** → **Properties** → tick **Unblock** →
**OK**, extract it somewhere you'll keep it, open the **Nexa** folder and double-click
**`start.bat`**. The first start runs the same setup.

## Share Nexa with another PC

On a PC where Nexa is installed, run:

```powershell
powershell -ExecutionPolicy Bypass -File make-package.ps1
```

It builds two files in `dist\`, with only the app's own files in them:
- **`NexaSetup.exe`** (about 2.5 MB): the installer. Share this one.
- **`Nexa.zip`** (about 2 MB): the same app as a plain folder.

Your API keys, chats, logs and downloaded models are **not** included. Each PC downloads
its own models during setup and adds its own keys. The installer is built with the C#
compiler that comes with Windows (`installer\NexaSetup.cs`), so nothing extra is needed.

Nexa knows she was made by Mic Dahryl Caybin on every PC. On the maker's own PC she also
knows who she's talking with: that's set by a file named `maker` in `%LOCALAPPDATA%\Nexa`,
which never goes into the installer.

## Talk to Nexa

In background listening, Nexa only answers what you say **to her**. Include her name
at the start or the end of each request:

- *"Hey Nexa, what's the weather today?"*
- *"Nexa, open Word for me"* or *"Nexa what is a black hole?"*
- *"Turn up the volume Nexa"*, *"Open Spotify, Nexa"* or *"What time is it, Nexa?"*
- Just *"Nexa"*: she answers *"Yes, Sir?"* and takes your next sentence as the request.

Talking *about* her, as in *"I read about Nexa today"*, doesn't wake her.
Nexa normally shows only her animated orb. The orb reacts while she listens, thinks,
and speaks. Normal conversation stays voice-only, with no chat box, menus, or settings.

- Say **"Hey Nexa"** with a request for a spoken answer.
- **Tap the orb** to start a continuous conversation without repeating her name.
- **Tap while she speaks** to interrupt her.
- Say **"goodbye"** or press **Esc** to end the conversation.
- On a new PC, tap the orb once and repeat one short sentence to set up your voice.
  Pause when you're done. Press **Esc** to cancel setup.

Follow-up context stays in memory while Nexa is open. Previously saved voice selections
and enrolled voice profiles continue to work. Her delivery is now Natural, with short,
casual replies and contractions instead of formal assistant language.

Her window keeps listening while it's minimized or behind other windows.

## Ask Nexa to show something

- **"Hey Nexa, show me the weather"** opens a hologram-style display with the requested answer.
- **"Can you show me a comparison table?"** shows a table; code requests can show a code block.
- **"Show your last answer"** puts the previous answer on screen without asking an AI again.
- **"Show me the current time"** opens a live local clock, even offline.
- **"Hide the display"**, **"clear the screen"**, or **"hide that"** closes the display.
  You can also use its close icon or press **Esc** (which also ends the voice conversation).

While content is displayed, the talking orb moves to the bottom and keeps reacting to
speech. Closing the display returns it to the center. The HUD remains available to read
while you continue talking, until you dismiss or replace it. Long content scrolls inside
the panel; tables and code can scroll horizontally. Available web sources appear as links.

The AI gives a short spoken takeaway separately from the full screen content, so it does
not read code or tables aloud. The display renders Markdown as safe text; generated code
is shown rather than executed. API keys and model selection remain in code.

## Commands: apps, websites, music

Tell Nexa to do something on the PC and she does it at once, even offline. She says what she
did (*"Opening Spotify, Sir."*). Say it to her like any request (*"Hey Nexa, open Spotify"*).

| Say | What happens |
| --- | --- |
| *open Spotify*, *open Discord*, *launch Calculator*, *open Word* | Opens any app in your Start menu. Small mishearings are fine (*"open spotfy"*) |
| *open YouTube*, *open Gmail*, *go to github.com* | Opens the website in your browser |
| *open Downloads*, *open the Recycle Bin*, *open Bluetooth settings* | Opens the folder or Settings page |
| *close Spotify*, *close Chrome* | Closes the app's windows like their X button; it can still ask to save |
| *play lofi beats on Spotify*, *search Spotify for Taylor Swift* | Opens Spotify's search for it (Spotify can't be told to start a song without its paid developer account, so click the one you want) |
| *play despacito on YouTube* | Starts the first video for it on YouTube |
| *play shape of you* | Spotify search if Spotify is installed, otherwise YouTube |
| *play*, *pause*, *next song*, *skip*, *previous song* | Controls whatever is playing: Spotify, YouTube, any player |
| *volume up* / *turn it down* / *louder* / *mute*, *set the volume to 30* | Changes the PC's volume |
| *what's this song?* | Reads the song from Spotify's window |
| *take a screenshot*, *show the desktop*, *lock the PC* | What it says |

Several at once work too: *"open Spotify and play lofi"*, *"open Notepad and Calculator"*.

She only does these things. She never runs a command an AI wrote, never deletes anything, and
won't shut down or restart the PC. Questions (*"how do I open Spotify?"*) still get a normal
answer. Play and pause are the keyboard's play/pause key, so *play* while music is already
playing pauses it.

## Configure AI in code

Edit **`nexa_config.py`** in the Nexa folder. If it isn't there, copy
`nexa_config.example.py` to `nexa_config.py` first:

```python
LOCAL_MODEL = "gemma3:1b"
OLLAMA_URL = "http://127.0.0.1:11434"
GROQ_API_KEY = ""
GEMINI_API_KEY = ""
CLAUDE_API_KEY = ""
ONLINE_BRAIN_ORDER = ("groq", "gemini", "claude")
```

Add your keys inside the quotes and set `LOCAL_MODEL` to an installed Ollama model.
Restart Nexa after editing. Configuration never appears in the app.

Every spoken request uses automatic AI selection. While online, Nexa tries available
models from configured providers in `ONLINE_BRAIN_ORDER`. A failed key, exhausted quota,
provider error, or empty answer moves to the next option. Without internet, or if every
online option fails, she uses `LOCAL_MODEL` on this PC. Offline requests skip online
model discovery. No model picker or settings screen is needed.

The private `nexa_config.py` file is excluded from Git and shared packages; only the
empty example is packaged. Environment variables (`ASSISTANT_MODEL`, `OLLAMA_URL`,
`GROQ_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) override the corresponding code
settings. Existing keys saved in Nexa's data folder are still supported.

## Web search

When web search is enabled in the saved preferences and the PC is online, Nexa searches
for questions that need fresh information: weather, news, prices, scores, "latest…",
"who is the current…", or anything starting with *"search for…"* or *"look up…"*.
Answers are read aloud in plain language. Other questions are answered without
searching. Search is enabled by default; it has no visible controls.

## Her look and sounds

- **Her orb, a cloud of teal light.** Thousands of softly glowing particles form a floating
  sphere, joined by fine nearby connections. A brighter, hazy center and a faint perspective
  floor give it depth. The cloud turns slowly and responds to your voice while listening and
  hers while speaking; waves of light travel through the particles on loud syllables. Thinking
  speeds up the rotation and brightens the connections. The orb settles beneath the display
  when you ask Nexa to show something. It's drawn on the graphics card (WebGL), with a matching
  particle effect on PCs without WebGL, and respects reduced-motion preferences.
- **Her logo:** a deep-green badge with the same hologram inside: the glowing core, its rings,
  and a tilted orbit carrying three stars. It's the app icon, the browser tab
  icon and the desktop shortcut.
- **Sound effects:** a crystal chime before she speaks, a chime when she hears her name, a
  soft swell when a voice conversation opens and closes, soft data ticks while she
  analyzes, and a ping when she searches. Existing sound-effect preferences are retained.
- When Nexa speaks, the central orb pulses with her voice. It stays on screen without subtitles.
- She talks like a relaxed, friendly companion: short, natural answers, with a bit of humor
  when it fits.

## Her hearing

On a new PC, tap the orb to enroll your voice with one short spoken prompt. First setup
installs [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) and downloads its 28 MB
English CAMPPlus speaker model. Listen to each sentence, repeat it aloud using your
usual microphone in a quiet room, then pause. Repeat just this sentence:
"Hey Nexa, this is my voice, and I'm ready to talk with you."
Nexa records it once and checks that it contains at least two seconds of speech
before saving the profile. Longer recordings are also checked for speaker changes.

Every spoken request is checked locally before transcription, web search, AI providers,
or PC commands. Without an enrolled voice, or if verification fails, spoken requests stay
blocked. Other voices are ignored, including wake-up calls. Say a complete request,
such as “Hey Nexa, can you open YouTube for me?” A single “Nexa” may be too short
to identify your voice reliably and can be ignored. She uses the full recording for
short requests; longer recordings also check four-second spans for speaker changes.
Tapping the orb starts a conversation that checks your voice but does not require her name.

The profile is stored in `%LOCALAPPDATA%\Nexa\owner_voice.json`, outside shared packages.
The animation-only app has no profile-management controls. An existing enrolled profile
is kept. During first-run enrollment, recordings stay in memory and are discarded after
the recording is accepted or setup is cancelled.
Speaker matching is a convenience filter, not authentication: recordings, cloned voices,
overlapping speakers, noise, and changes to your microphone can cause mistakes. It hasn't
been calibrated to your voice until you enroll and try it. Don't use it as a security lock.

After updating an older Nexa folder, `start.bat` runs setup once to add speaker recognition.
If downloads fail, connect to the internet and run `setup.ps1` again.

After the speaker check, **Hearing: Accurate** uses the bigger speech model (`small.en`)
for both wake phrases and requests. **Fast** uses the smaller `base.en` model. Accurate
stays enabled by default even on CPU, where it takes longer. If the transcript is too
uncertain, Nexa asks you to repeat instead of acting on a partial command. While she
speaks, listening pauses briefly to avoid recognizing her own voice as your request.

She waits for about **1.8 seconds of silence** before finishing a turn, so a short
pause can stay inside the same request. Quiet words after a loud first word are kept,
and delayed browser callbacks don't count as silence. A spoken turn finishes automatically after a pause. Spoken turns can last about **35 seconds**;
if the recording reaches that limit, she asks for a shorter request and doesn't act on
the unfinished sentence.

Transcripts are checked word by word as well as phrase by phrase. In Accurate mode,
an uncertain result gets one extra decoding attempt for softer speech. If it is still
unclear, the whole turn is rejected and she asks you to repeat. Spoken replies also
use the conversation history for follow-ups and are instructed to ask when a detail
is ambiguous. These checks reduce guessing; they don't guarantee an exact transcript.

Why she still mishears sometimes:
- The models are small enough to run on a laptop, so they make more mistakes than cloud
  assistants.
- A laptop's built-in microphone picks up room echo and fan noise. A headset or USB
  microphone helps the most.
- Strong accents, fast speech, music or a TV in the background all lower accuracy.

Speak clearly, about an arm's length from the microphone, and pause briefly after her name.

To run the speech checks without using your microphone or enrolling a voice:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node --test tests/voice-ui.test.cjs tests/hud.test.cjs
.\.venv\Scripts\python.exe tests/smoke_speech.py
```

The last check uses the locally installed speech and voice models. It compares the
recognized words against generated English speech, including a quiet recording,
a British voice, a negation, and a pause between sentences; it also checks silence
and noise. It doesn't measure accuracy for your own voice, accent, or room.

## Advanced settings

Set these environment variables before running `start.bat`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASSISTANT_MODEL` | `gemma3:1b` | offline model (`gemma3:4b` is smarter but needs a faster PC; download it with `ollama pull gemma3:4b`) |
| `CODE_MODEL` | `qwen2.5-coder:7b` | offline model for pygame/VPython questions, used only if it's installed (`gemma3:4b` is smaller and faster but writes worse code) |
| `CODE_KEEP_ALIVE` | `5m` | how long the code model stays loaded after an answer (it needs about 5 GB) |
| `NEXA_VOICE` | `en_GB-cori-high` | default Piper voice in the `voices` folder |
| `NEXA_PACE` | `1.08` | speaking pace (higher is slower and calmer) |
| `WHISPER_MODEL` | `base.en` | fast hearing model |
| `WHISPER_ACCURATE_MODEL` | `small.en` | accurate hearing model |
| `NEXA_STT_DEVICE` | `auto` | `auto` uses an NVIDIA card when it works; `cpu` forces the processor |
| `GEMINI_API_KEY` | (none) | Gemini key; overrides code configuration and previously saved keys |
| `GROQ_API_KEY` | (none) | Groq key; overrides code configuration and previously saved keys |
| `ANTHROPIC_API_KEY` | (none) | Claude key; overrides code configuration and previously saved keys |
| `NEXA_BROWSER` | (her own window) | `default` opens her in your normal browser instead |
| `PORT` | `8000` | web page port |

More voices: any [Piper voice](https://huggingface.co/rhasspy/piper-voices) works.
For example, Amy (female, US):

```powershell
.\.venv\Scripts\python.exe -m piper.download_voices en_US-amy-medium --data-dir voices
```

Set `NEXA_VOICE` to the downloaded voice ID before starting Nexa to use it.

## Troubleshooting

Nexa runs without a console window. Everything she hears, and her microphone's status, is
saved to `logs\nexa.log`:

```
18:19:30 heard[wake] 'Hey Nexa, what is 2 plus 2?'  (2.5s of audio, waited 0.0s, took 1.4s)
18:24:40 page mic-open {"device": "Microphone (Realtek High Definition Audio)", ...}
```

To watch it live, quit Nexa and start her from her folder with
`.venv\Scripts\python.exe server.py --open`, which shows it in a console window.

- **She never reacts:** check that a `mic-open` line appears and that `heard[wake]` lines
  show your words. Check that Windows allows apps to use
  the microphone (Settings → Privacy → Microphone).
- **"Ollama not running":** start Ollama from the Start menu, or run `start.bat` again.
- **To stop Nexa completely,** end its Python process in Windows Task Manager. Closing
  only her window keeps her running in the background; the shortcut brings it back.
- **Moved the Nexa folder?** Delete the `.venv` folder inside it and run `start.bat`
  again, then use the new shortcut.

## Uninstall

1. Installed with NexaSetup.exe: open **Settings → Apps**, find **Nexa** and choose
   **Uninstall**. It asks whether to delete your saved keys and chats too.
   From the zip: delete the Nexa folder and the Nexa shortcuts (desktop and Start menu),
   and `%LOCALAPPDATA%\Nexa` (saved keys, chats and her window's settings).
2. Ollama stays installed either way.
3. Optional: uninstall **Ollama** in Windows Settings → Apps (its models are in
   `%USERPROFILE%\.ollama`), and delete the hearing models in
   `%USERPROFILE%\.cache\huggingface`.

## Limits

- Small offline models make more mistakes than cloud AI, and may summarize search
  results imperfectly; check the sources for anything important.
- From the end of your sentence to her first word takes about 2 to 3 seconds, or longer
  when she searches the web or runs on a PC without an NVIDIA card.
