"""Nexa: a Gemini-style assistant that listens, talks and searches the web when online.

Chat runs on a local model served by Ollama (Gemma by default), speech-to-text on
faster-whisper and Nexa's voice on Piper, so everything works offline. When the
PC is online, questions about news, weather or other current facts are answered
with a DuckDuckGo web search.

    python server.py             start the app on http://127.0.0.1:8000
    python server.py --open      start the app and open Nexa's window
    pythonw server.py --launch   what the Nexa shortcut runs: no console window; starts Ollama if
                                 needed, and if Nexa is already running just opens her window
    python server.py --prefetch  download the speech and voice models, then exit
    python server.py --window    open Nexa's window for an app that's already running
"""
import ctypes
import base64
import binascii
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Started by pythonw (the shortcut) there's no console, and uvicorn fails without one. Printed
# lines are already saved to logs/nexa.log by log(), so drop them; errors go to the same log.
if sys.stdout is None or sys.stderr is None:
    (Path(__file__).parent / "logs").mkdir(exist_ok=True)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(Path(__file__).parent / "logs" / "nexa.log", "a", encoding="utf-8", buffering=1)

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from owner_voice import OwnerVoice

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

try:
    import nexa_config as local_config
except ModuleNotFoundError as error:
    if error.name != "nexa_config":
        raise
    local_config = None

OLLAMA_URL = os.environ.get("OLLAMA_URL", getattr(local_config, "OLLAMA_URL", "http://127.0.0.1:11434"))
DEFAULT_MODEL = os.environ.get("ASSISTANT_MODEL", getattr(local_config, "LOCAL_MODEL", "gemma3:1b"))
ONLINE_BRAIN_ORDER = getattr(local_config, "ONLINE_BRAIN_ORDER", ("groq", "gemini", "claude"))
# pygame/VPython questions answered on this PC go to this bigger model when it's installed
# (`ollama pull qwen2.5-coder:7b`); it writes far better code than the 1B, but slowly.
CODE_MODEL = os.environ.get("CODE_MODEL", "qwen2.5-coder:7b")
CODE_KEEP_ALIVE = os.environ.get("CODE_KEEP_ALIVE", "5m")  # unload it sooner: it needs about 5 GB of memory
# Two speech models: a fast one checks every phrase for her name, an accurate one
# re-reads the phrases meant for her. English-only models recognize her name far
# more reliably than multilingual ones; set both to "base"/"small" for other languages.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_ACCURATE_MODEL = os.environ.get("WHISPER_ACCURATE_MODEL", "small.en")
VOICE_NAME = os.environ.get("NEXA_VOICE", "en_GB-cori-high")  # Nexa speaks with a soft British female voice
SPEECH_PACE = float(os.environ.get("NEXA_PACE", "1.08"))  # >1 is slower, calmer speech
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "30m")  # how long Ollama keeps the model loaded
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SAMPLE_RATE = 16000
VERSION = "1.0"
MAKER = "Mic Dahryl Caybin"

SYSTEM_PROMPT = (
    f"You are Nexa, a friendly AI (she/her) that lives inside the user's computer, created by {MAKER}, "
    f"who designed and built you and keeps improving you. If anyone asks who made, created, built "
    f"or owns you, say {MAKER} (call them by name rather than he or she). The AI models you think with (Gemma, GPT-OSS on Groq, Gemini or "
    "Claude) are only engines you run on, so never say a company such as Google, OpenAI, Meta or "
    "Anthropic made you. Talk like a relaxed, "
    "warm friend: casual, natural sentences with contractions, a little humor when it fits, and "
    "keep it brief. Sound like someone having a conversation, not reading a formal report. "
    "Skip stock openings like 'Certainly' and 'As an AI', avoid forced slang, and don't repeat "
    "the user's title or name in every answer. Use everyday words and match their tone. "
    "Use Markdown for lists, and write code only when the user asks for it. If "
    "you're unsure, say so instead of guessing. When web search results are provided, rely on "
    "them for current facts. Without them you can't know live information such as news, "
    "weather or prices. Plain PC commands (like 'open Spotify', 'play lofi on YouTube', 'next "
    "song', 'volume up', 'close Chrome', 'lock the PC') are carried out by the computer before "
    "you see them. You can't do anything on the PC yourself, so never say you did; if asked, "
    "suggest saying it as a short command like those."
)
VOICE_HINT = (
    " The user is talking to you by voice and your reply will be read aloud, so "
    "answer in one to three relaxed spoken sentences by default, using contractions and "
    "everyday language, without Markdown, lists, code or "
    "source numbers. Follow the conversation history when the user refers to something "
    "said earlier. Their latest correction replaces the earlier detail. Speech transcripts "
    "can contain mistakes: if a request is incomplete or ambiguous, ask one short "
    "clarifying question. Never invent missing words, names, numbers or intentions."
)
DISPLAY_HINT = (
    " The user explicitly asked to see something on Nexa's display. Give TWO parts, exactly: "
    "<voice>A short, casual spoken explanation of the main takeaway, one or two sentences. "
    "Do not read out the screen, URLs, code, or table rows.</voice>\n"
    "<display># A short, useful title\nThe requested content in Markdown. Use clear paragraphs, "
    "lists, tables, or fenced code when they help. Show the actual answer, not instructions to "
    "look elsewhere. Only include facts and sources you have; never invent live data. "
    "If you cannot get requested data, say so clearly.</display> "
    "These tags route speech and screen content separately. Do not put the tags in a code fence."
)


def split_display_reply(text: str) -> tuple[str, str]:
    """Keep presentation markup out of spoken replies, including imperfect local output."""
    voice = re.search(r"<voice>\s*(.*?)\s*</voice>", text, re.S | re.I)
    screen = re.search(r"<display>\s*(.*?)(?:</display>|$)", text, re.S | re.I)
    content = screen.group(1).strip() if screen else re.sub(
        r"<voice>.*?(?:</voice>|$)", "", text, flags=re.S | re.I
    ).strip()
    content = re.sub(r"</?(?:voice|display)>", "", content, flags=re.I).strip()
    speech = voice.group(1).strip() if voice else "Here you go. I've put it up for you."
    # A malformed response must never make the TTS read code or a long report.
    if not speech or len(speech) > 450 or re.search(r"```|<[^>]+>|https?://|\|", speech):
        speech = "Here you go. I've put it up for you."
    return speech, content or "I couldn't get the display content. Try asking again."


async def display_reply_stream(events):
    """Preserve status/source events; deliver distinct screen and spoken content."""
    parts = []
    async for line in events:
        event = json.loads(line)
        if isinstance(event.get("t"), str):
            parts.append(event["t"])
        else:
            yield line
    if parts:
        speech, content = split_display_reply("".join(parts))
        yield json.dumps({"display": {"content": content}}) + "\n"
        yield json.dumps({"t": speech}) + "\n"

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
VOICES_DIR = ROOT / "voices"
VOICE_PATH = VOICES_DIR / f"{VOICE_NAME}.onnx"
LOG_PATH = ROOT / "logs" / "nexa.log"

# How each speaking style drives Piper. "calm" lowers the random variation in pitch and
# timing, which gives an even, steady delivery.
VOICE_STYLES = {
    "calm": {"length_scale": SPEECH_PACE * 1.04, "noise_scale": 0.33, "noise_w_scale": 0.4},
    "natural": {"length_scale": 1.0, "noise_scale": 0.667, "noise_w_scale": 0.8},
}
VOICE_LABELS = {
    "en_GB-alan-medium": "Alan · male, calm (UK)",
    "en_US-ryan-medium": "Ryan · male (US)",
    "en_US-lessac-medium": "Lessac · female, clear (US)",
    "en_US-hfc_female-medium": "HFC · female, calm (US)",
    "en_US-amy-medium": "Amy · female, flat (US)",
    "en_GB-cori-high": "Cori · female, soft (UK)",
}

# Casual voices from Kokoro (Apache-2.0): they sound like someone talking rather than reading.
KOKORO_DIR = VOICES_DIR / "kokoro"
KOKORO_FILES = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_VOICES = {
    "kokoro:am_puck": "Puck · male (US) · casual",
    "kokoro:am_michael": "Michael · male (US) · casual",
    "kokoro:am_eric": "Eric · male (US) · casual",
    "kokoro:bm_lewis": "Lewis · male (UK) · casual",
    "kokoro:af_heart": "Heart · female (US) · casual",
    "kokoro:bf_emma": "Emma · female (UK) · casual",
}
VOICE_LABELS.update(KOKORO_VOICES)
_kokoro = {}  # device -> Kokoro

_whispers = {}  # model name -> WhisperModel
_whisper_lock = threading.Lock()
_stt_device = {}  # model name -> "cuda" or "cpu"
_pipers = {}
_piper_lock = threading.Lock()
_tts_lock = threading.Lock()
_model_ready = False
_log_lock = threading.Lock()


def log(message: str):
    """Print to Nexa's console window and append to logs/nexa.log (for troubleshooting)."""
    line = f"{datetime.now():%H:%M:%S} {message}"
    print(line, flush=True)
    with _log_lock:
        LOG_PATH.parent.mkdir(exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def installed_voices() -> list[str]:
    piper = sorted(p.stem for p in VOICES_DIR.glob("*.onnx") if p.with_suffix(".onnx.json").exists())
    return piper + (list(KOKORO_VOICES) if kokoro_model() and kokoro_gpu() else [])


def kokoro_model() -> Path | None:
    """Kokoro's model file, if it's downloaded (and the kokoro-onnx package is installed)."""
    import importlib.util

    model = KOKORO_DIR / "kokoro-v1.0.onnx"
    if not model.exists() or not (KOKORO_DIR / "voices-v1.0.bin").exists() or not importlib.util.find_spec("kokoro_onnx"):
        return None
    return model


def kokoro_gpu() -> bool:
    """Whether Kokoro can run on an NVIDIA card. It's too slow on a processor to talk smoothly, so
    its voices are only offered with the GPU build of ONNX Runtime (setup installs it on NVIDIA PCs)."""
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    return "CUDAExecutionProvider" in ort.get_available_providers() and os.environ.get("NEXA_STT_DEVICE") != "cpu"


class _ReturnsMemory:
    """Kokoro's ONNX session, made to hand its working memory back to the graphics card after each
    sentence, so the hearing and the next sentence have room on a small card."""

    def __init__(self, session):
        import onnxruntime as ort

        self._session = session
        self._options = ort.RunOptions()
        self._options.add_run_config_entry("memory.enable_memory_arena_shrinkage", "gpu:0")

    def run(self, output_names, input_feed, run_options=None):
        return self._session.run(output_names, input_feed, self._options)

    def __getattr__(self, name):
        return getattr(self._session, name)


def get_kokoro(device: str = "cuda"):
    """Load Kokoro once per device ("cuda", or "cpu" as a fallback) and keep it in memory."""
    with _piper_lock:
        if device not in _kokoro:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            providers = ["CPUExecutionProvider"]
            if device == "cuda":
                ort.preload_dlls()  # NVIDIA's libraries installed with pip
                # A 2 GB card is shared with hearing and the brain: grow memory only as needed, and cap it.
                lean = {"arena_extend_strategy": "kSameAsRequested", "cudnn_conv_algo_search": "HEURISTIC",
                        "gpu_mem_limit": str(1000 * 1024 * 1024)}
                providers.insert(0, ("CUDAExecutionProvider", lean))
            session = ort.InferenceSession(str(kokoro_model()), providers=providers)
            if device == "cuda":
                session = _ReturnsMemory(session)
            _kokoro[device] = Kokoro.from_session(session, str(KOKORO_DIR / "voices-v1.0.bin"))
            log(f"casual voices (Kokoro) ready on {'cuda' if 'CUDA' in session.get_providers()[0] else 'cpu'}")
    return _kokoro[device]


def _use_pip_cuda_libraries():
    """Let CTranslate2 find NVIDIA's cuBLAS/cuDNN DLLs installed with pip, if present."""
    import importlib.util

    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(pkg)
        for base in (spec.submodule_search_locations or []) if spec else []:
            dll_dir = Path(base) / "bin"
            if dll_dir.is_dir():
                os.add_dll_directory(str(dll_dir))
                os.environ["PATH"] = f"{dll_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _load_whisper(name: str):
    from faster_whisper import WhisperModel

    def load(**kw):
        try:
            return WhisperModel(name, local_files_only=True, **kw)
        except Exception as e:
            if "cuda" in str(kw).lower() or "local" not in str(e).lower():
                raise
            # Not cached yet: this is the only step that needs internet.
            return WhisperModel(name, **kw)

    # Use the graphics card when it works; fall back to the processor otherwise.
    if os.environ.get("NEXA_STT_DEVICE", "auto") != "cpu":
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                _use_pip_cuda_libraries()
                model = load(device="cuda", compute_type="int8")
                # Decoding is lazy: consume the generator to actually exercise CUDA.
                segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, np.float32), beam_size=1)
                list(segments)
                _stt_device[name] = "cuda"
                return model
        except Exception as e:
            log(f"speech model {name}: graphics card unavailable ({str(e)[:120]}), using the processor")
    _stt_device[name] = "cpu"
    return load(device="cpu", compute_type="int8")


def get_whisper(name: str = WHISPER_MODEL):
    """Load a speech model once, preferring the local cache so it works offline."""
    with _whisper_lock:
        if name not in _whispers:
            _whispers[name] = _load_whisper(name)
            log(f"speech model {name} ready on {_stt_device[name]}")
        return _whispers[name]


def get_piper(name: str = VOICE_NAME):
    """Load a voice once and keep it in memory."""
    with _piper_lock:
        if name not in _pipers:
            from piper import PiperVoice

            _pipers[name] = PiperVoice.load(VOICES_DIR / f"{name}.onnx")
        return _pipers[name]


def warm_model():
    """Ask Ollama to load the default model now; a cold load can take a minute."""
    global _model_ready
    try:
        httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": DEFAULT_MODEL, "keep_alive": KEEP_ALIVE},
            timeout=300,
        )
    except httpx.HTTPError:
        pass  # Ollama not running yet; /api/status reports it to the page
    _model_ready = True


# ---------- Web search (only used when the PC is online) ----------

_online = {"ok": False, "checked": 0.0}


async def check_online(max_age: float = 30) -> bool:
    if time.monotonic() - _online["checked"] < max_age:
        return _online["ok"]
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.head("https://duckduckgo.com")
        ok = True
    except httpx.HTTPError:
        ok = False
    _online.update(ok=ok, checked=time.monotonic())
    return ok


SEARCH_CMD_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:please\s+)?"
    r"(?:search(?:\s+(?:the\s+)?(?:web|internet|online))?(?:\s+for)?|look\s+up|google|"
    r"find\s+(?:online|on\s+the\s+(?:web|internet)))\s+",
    re.I,
)
WEATHER_RE = re.compile(
    r"\b(weather|forecast|temperature|raining|rain|snow(?:ing)?|sunny|humid(?:ity)?|typhoon|storm)\b",
    re.I,
)
NEWS_RE = re.compile(r"\b(news|headlines?|breaking|what(?:'s| is) happening)\b", re.I)
FRESH_RE = re.compile(
    r"\b(latest|today|tonight|tomorrow|yesterday|this (?:week|weekend|month|year)|"
    r"current(?:ly)?|right now|recent(?:ly)?|upcoming|prices?|stock|bitcoin|crypto|"
    r"exchange rate|scores?|who won|winner|election|release date|trending|20[2-3]\d)\b|"
    r"\b(?:who is|who's) (?:the )?(?:current |new )?"
    r"(?:president|prime minister|ceo|king|queen|leader|champion|mayor|governor)\b|"
    r"\b(?:online|on the (?:web|internet))\b",
    re.I,
)
CREATIVE_RE = re.compile(r"\b(write|poem|story|code|function|program|translate|summari[sz]e this)\b", re.I)
TIME_WORDS = {"today", "tomorrow", "tonight", "now", "right now", "this week", "this weekend", "later"}
LOCATION_RE = re.compile(
    r"\b(?:in|at|for|around)\s+([a-z][a-z .,'-]{1,60}?)\s*"
    r"(?=\b(?:today|tomorrow|tonight|right now|now|this week(?:end)?|later)\b|[?.!]|$)",
    re.I,
)
NEWS_FILLER_RE = re.compile(
    r"\b(what(?:'s| is| are)?|tell me|give me|any|the|latest|top|news|headlines?|breaking|"
    r"today(?:'s)?|about|on|for|me|please|is there|are there|some)\b",
    re.I,
)


def _clean_query(text: str) -> str:
    text = re.sub(r"\b(?:please|for me|hey nexa)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ?.!,")


def plan_search(text: str):
    """Decide whether a message needs the web. Returns None or {kind, query, location}."""
    command = SEARCH_CMD_RE.match(text)
    if command:
        query = _clean_query(text[command.end():])
        return {"kind": "news" if NEWS_RE.search(query) else "web", "query": query or _clean_query(text)}
    if WEATHER_RE.search(text):
        m = LOCATION_RE.search(text)
        location = m.group(1).strip(" ,.") if m else ""
        if location.lower() in TIME_WORDS:
            location = ""
        return {"kind": "weather", "query": text, "location": location}
    if NEWS_RE.search(text):
        topic = re.sub(r"\s+", " ", NEWS_FILLER_RE.sub(" ", text)).strip(" ?.!,'")
        return {"kind": "news", "query": f"{topic} news" if topic else "top news"}
    if FRESH_RE.search(text) and not CREATIVE_RE.search(text):
        return {"kind": "web", "query": _clean_query(text)}
    return None


def _weather(location: str):
    url = f"https://wttr.in/{quote(location)}?format=j1"
    data = httpx.get(url, timeout=10, headers={"User-Agent": "curl/8"}).json()
    now = data["current_condition"][0]
    area = data.get("nearest_area", [{}])[0]
    found = [v[0]["value"] for k in ("areaName", "region", "country") if (v := area.get(k)) and v[0]["value"]]
    # wttr.in names the nearest weather station; show the city that was asked for.
    place = f"{location.title()}, {found[-1]}" if location and found else ", ".join(found) or "your area"
    lines = [
        f"Weather for {place}:",
        f"Now: {now['weatherDesc'][0]['value'].strip()}, {now['temp_C']}°C "
        f"(feels like {now['FeelsLikeC']}°C), humidity {now['humidity']}%, "
        f"wind {now['windspeedKmph']} km/h.",
    ]
    for day in data.get("weather", [])[:3]:
        hourly = day.get("hourly", [])
        midday = hourly[len(hourly) // 2] if hourly else {}
        rain = max((int(h.get("chanceofrain", 0)) for h in hourly), default=0)
        desc = midday.get("weatherDesc", [{"value": ""}])[0]["value"].strip()
        lines.append(f"{day['date']}: {desc}, {day['mintempC']}–{day['maxtempC']}°C, chance of rain {rain}%.")
    return [{"title": f"Weather · {place}", "url": f"https://wttr.in/{quote(location)}"}], "\n".join(lines)


def _wikipedia(query: str):
    r = httpx.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 3},
        timeout=8,
        headers={"User-Agent": "Nexa/1.0 (local assistant)"},
    )
    items = []
    for hit in r.json().get("query", {}).get("search", []):
        title = hit["title"]
        snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
        items.append({"title": f"{title} · Wikipedia", "url": f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}", "snippet": snippet})
    return items


def _web(query: str, news: bool):
    items = []
    try:
        from ddgs import DDGS

        ddgs = DDGS(timeout=8)
        if news:
            for r in ddgs.news(query, max_results=6):
                when = (r.get("date") or "")[:10]
                src = r.get("source") or ""
                items.append({"title": r["title"], "url": r["url"], "snippet": r.get("body", ""), "meta": ", ".join(x for x in (src, when) if x)})
        else:
            for r in ddgs.text(query, max_results=6):
                items.append({"title": r["title"], "url": r["href"], "snippet": r.get("body", "")})
    except Exception:
        items = []
    if not items:
        items = _wikipedia(query)
    lines = [f'Web search results for "{query}":']
    for i, it in enumerate(items, 1):
        meta = f" ({it['meta']})" if it.get("meta") else ""
        lines.append(f"[{i}] {it['title']}{meta}: {it['snippet']}")
    return [{"title": it["title"], "url": it["url"]} for it in items], "\n".join(lines)


def run_search(plan: dict):
    if plan["kind"] == "weather":
        try:
            return _weather(plan["location"])
        except Exception:
            pass  # fall back to a normal web search
    return _web(plan["query"], news=plan["kind"] == "news")


# ---------- PC commands: open apps and websites, music and volume keys ----------
#
# Commands are recognized by rules before any AI brain sees the message, so they work offline
# and instantly. Only a fixed set of safe actions exists: open something from the Start menu, a
# folder, a Settings page or a web address; press the media and volume keys; close an app's
# windows the way its X button does (it can still ask to save); lock the PC. Nothing the AI
# writes is ever run.

WINDOWS = sys.platform == "win32"
_apps = {"list": [], "at": 0.0}
_apps_lock = threading.Lock()
NOT_APP_RE = re.compile(r"\b(?:uninstall\w*|setup|installer|repair)\b", re.I)

APP_ALIASES = {
    "chrome": "google chrome", "edge": "microsoft edge", "explorer": "file explorer",
    "files": "file explorer", "my files": "file explorer", "file manager": "file explorer",
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "calc": "calculator", "cmd": "command prompt", "terminal": "windows terminal",
    "store": "microsoft store", "app store": "microsoft store", "control panel": "control panel",
    "notepad plus plus": "notepad++", "powershell": "windows powershell", "teams": "microsoft teams",
    "ms word": "word", "microsoft word": "word", "microsoft excel": "excel",
    "microsoft powerpoint": "powerpoint", "power point": "powerpoint", "snipping tool": "snipping tool",
}
SITES = {
    "youtube": ("YouTube", "https://www.youtube.com"), "you tube": ("YouTube", "https://www.youtube.com"),
    "google": ("Google", "https://www.google.com"), "gmail": ("Gmail", "https://mail.google.com"),
    "google drive": ("Google Drive", "https://drive.google.com"), "drive": ("Google Drive", "https://drive.google.com"),
    "google docs": ("Google Docs", "https://docs.google.com"), "google maps": ("Google Maps", "https://maps.google.com"),
    "maps": ("Google Maps", "https://maps.google.com"), "google calendar": ("Google Calendar", "https://calendar.google.com"),
    "google translate": ("Google Translate", "https://translate.google.com"),
    "facebook": ("Facebook", "https://www.facebook.com"), "messenger": ("Messenger", "https://www.messenger.com"),
    "instagram": ("Instagram", "https://www.instagram.com"), "twitter": ("X", "https://x.com"), "x": ("X", "https://x.com"),
    "tiktok": ("TikTok", "https://www.tiktok.com"), "tik tok": ("TikTok", "https://www.tiktok.com"),
    "netflix": ("Netflix", "https://www.netflix.com"), "reddit": ("Reddit", "https://www.reddit.com"),
    "github": ("GitHub", "https://github.com"), "chatgpt": ("ChatGPT", "https://chatgpt.com"),
    "chat gpt": ("ChatGPT", "https://chatgpt.com"), "claude": ("Claude", "https://claude.ai"),
    "wikipedia": ("Wikipedia", "https://www.wikipedia.org"), "linkedin": ("LinkedIn", "https://www.linkedin.com"),
    "outlook": ("Outlook", "https://outlook.live.com"), "amazon": ("Amazon", "https://www.amazon.com"),
    "spotify": ("Spotify", "https://open.spotify.com"), "discord": ("Discord", "https://discord.com/app"),
    "twitch": ("Twitch", "https://www.twitch.tv"), "pinterest": ("Pinterest", "https://www.pinterest.com"),
    "whatsapp": ("WhatsApp", "https://web.whatsapp.com"), "canva": ("Canva", "https://www.canva.com"),
}
FOLDERS = {
    "downloads": ("your Downloads", "shell:Downloads"), "documents": ("your Documents", "shell:Personal"),
    "desktop folder": ("your Desktop", "shell:Desktop"), "pictures": ("your Pictures", "shell:My Pictures"),
    "music folder": ("your Music", "shell:My Music"), "videos": ("your Videos", "shell:My Video"),
    "this pc": ("This PC", "shell:MyComputerFolder"), "my computer": ("This PC", "shell:MyComputerFolder"),
    "recycle bin": ("the Recycle Bin", "shell:RecycleBinFolder"), "trash": ("the Recycle Bin", "shell:RecycleBinFolder"),
}
SETTINGS_PAGES = {
    "bluetooth": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi", "wi fi": "ms-settings:network-wifi",
    "network": "ms-settings:network", "internet": "ms-settings:network", "display": "ms-settings:display",
    "sound": "ms-settings:sound", "audio": "ms-settings:sound", "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep", "windows update": "ms-settings:windowsupdate", "update": "ms-settings:windowsupdate",
    "apps": "ms-settings:appsfeatures", "notifications": "ms-settings:notifications",
    "microphone": "ms-settings:privacy-microphone", "mic": "ms-settings:privacy-microphone",
    "privacy": "ms-settings:privacy", "wallpaper": "ms-settings:personalization-background",
    "background": "ms-settings:personalization-background", "date and time": "ms-settings:dateandtime",
}
CLOSE_EXES = {"word": "winword", "powerpoint": "powerpnt", "edge": "msedge", "microsoft edge": "msedge",
              "google chrome": "chrome", "visual studio code": "code", "file explorer": "explorer",
              "task manager": "taskmgr", "notepad++": "notepad++"}
NOT_CLOSABLE = {"yourself", "you", "nexa", "this", "it", "that", "everything", "all", "chat", "the chat",
                "conversation", "the conversation", "session", "this session", "the session", "window", "the window"}
NUMBER_WORDS = {"zero": 0, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "thirty": 30, "forty": 40,
                "fifty": 50, "half": 50, "sixty": 60, "seventy": 70, "seventy five": 75, "eighty": 80,
                "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100, "max": 100,
                "maximum": 100, "full": 100}

LEAD_WORDS = (r"please|kindly|just|now|so|and|ok(?:ay)?|alright|hey|go ahead and|"
              r"(?:can|could|would|will) you|i want you to|i'?d like you to|i need you to|you can")
OPEN_RE = re.compile(r"^(open|launch|start|run|fire up|bring up|pull up|go to|visit)(?: up)?\s+(.+)$")
CLOSE_RE = re.compile(r"^(?:close|quit|exit|kill)\s+(.+)$")
PLATFORM_RE = re.compile(
    r"^(?:play|put on|listen to|open|search(?: for)?|find|look up|show(?: me)?|watch)\s+(.+?)\s+"
    r"(?:on|in|from|using|with)\s+(spotify|youtube|you tube)$")
PLATFORM_FIRST_RE = re.compile(r"^(?:search|look up|find)\s+(?:on\s+)?(spotify|youtube|you tube)\s+for\s+(.+)$")
PLAY_ANY_RE = re.compile(r"^(?:play|put on)\s+(.+)$")
NOT_SONG_RE = re.compile(
    r"\b(?:game|games|with me|quiz|trivia|questions?|riddles?|pretend|chess|tic tac toe|rock paper|"
    r"role|a round|along|hard to get|fair|dead|nice|it cool|a trick|tricks|a joke|jokes)\b")
MUSIC_WORDS = r"(?: (?:the |my |some |this |a )?(?:music|song|track|audio|spotify|playback|video|it|that))?"
MEDIA = [
    ("pause", re.compile(rf"^pause{MUSIC_WORDS}$|^stop (?:the |my |this )?(?:music|song|track|audio|spotify|"
                         rf"playback|video)$|^stop playing(?: (?:the )?music)?$")),
    ("play", re.compile(rf"^(?:play|resume|unpause){MUSIC_WORDS}$|^(?:continue|keep) (?:playing|the music|the song)$")),
    ("next", re.compile(r"^(?:(?:play |go to |skip to )?(?:the )?next (?:song|track|one|video)|"
                        r"skip(?: (?:this|that|the))?(?: (?:song|track|one|video|it))?|change (?:the |this )?(?:song|track))$")),
    ("previous", re.compile(r"^(?:(?:play |go (?:back )?to )?(?:the )?(?:previous|last|prior) (?:song|track|one|video)|"
                            r"go back(?: a| one)? (?:song|track)|previous|back to the last (?:song|track))$")),
    ("up", re.compile(r"^(?:(?:turn|crank|bump|pump|push)(?: (?:it|the volume|the music|the sound|this|that))? up"
                      r"(?: (?:the )?(?:volume|music|sound))?|(?:volume|sound) up|(?:increase|raise|boost) (?:the )?"
                      r"(?:volume|sound)|(?:a (?:bit|little) )?louder|make it (?:a (?:bit|little) )?louder)"
                      r"(?: (?:a bit|a little|a lot|more))?$")),
    ("down", re.compile(r"^(?:(?:turn)(?: (?:it|the volume|the music|the sound|this|that))? down"
                        r"(?: (?:the )?(?:volume|music|sound))?|(?:volume|sound) down|(?:decrease|lower|reduce|drop) "
                        r"(?:the )?(?:volume|sound)|(?:a (?:bit|little) )?(?:quieter|softer)|"
                        r"make it (?:a (?:bit|little) )?(?:quieter|softer))(?: (?:a bit|a little|a lot|more))?$")),
    ("mute", re.compile(r"^(?:un)?mute(?: (?:the )?(?:volume|sound|audio|pc|computer|music|it|everything|speakers?))?$")),
]
VOLUME_SET_RE = re.compile(
    r"^(?:(?:set|change|put|turn|make)(?: the)? (?:volume|sound)(?: level)? (?:to|at)|(?:volume|sound) (?:to|at)?|"
    r"(?:max|full) volume)\s*(.*?)(?: ?%| percent)?$")
LOCK_RE = re.compile(r"^lock (?:the |my |this )?(?:pc|computer|screen|laptop|windows|device)$")
SNIP_RE = re.compile(r"^(?:(?:take|grab|capture|make) (?:a )?(?:screenshot|screen ?shot|screen capture|snip)"
                     r"(?: of (?:the|my) screen)?|screenshot)$")
DESKTOP_RE = re.compile(r"^(?:(?:show|go to) (?:the |my )?desktop|minimi[sz]e (?:all|everything|all (?:the |my )?windows))$")
POWER_RE = re.compile(r"^(?:shut ?down|turn off|power off|restart|reboot|log ?off|log out|sign out|hibernate|put)"
                      r"(?: (?:the|my|this))? (?:pc|computer|laptop|windows|system|device)(?: to sleep)?$")
NOW_PLAYING_RE = re.compile(r"^(?:what(?:'s| is)(?: this| the| that)? (?:song|track)(?: (?:this|playing|is this|"
                            r"is playing|is that))?|what(?:'s| is) playing|what song is (?:this|playing|that)|"
                            r"which song is this|what am i listening to|who sings this(?: song)?)$")


def _norm(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#' ]+", " ", text).replace("'", "")).strip()


def _clean_command(text: str) -> str:
    c = text.strip().lower().replace("’", "'")
    c = re.sub(r"[“”\"]", "", c)
    c = re.sub(r"^(?:(?:hey|hi|ok(?:ay)?|yo)[\s,]+)?nexa\b[\s,.!:]*", "", c).strip(" .!?,")
    c = re.sub(rf"^(?:(?:{LEAD_WORDS})[\s,]+)+", "", c)
    # Her name can also come last ("open Spotify, Nexa", "open Word for me Nexa please").
    c = re.sub(r"(?:[\s,]+(?:please|for me|right now|now|thanks|thank you|real quick|quickly|again|nexa))+$", "", c)
    return re.sub(r"\s+", " ", c).strip(" .!?,")


def _read_start_apps():
    """[(name, target)] for every app in the Start menu. Targets open with os.startfile."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        apps = json.loads(out.stdout.decode("utf-8", errors="replace") or "[]")
        apps = [apps] if isinstance(apps, dict) else apps
        found = [(a["Name"], f"shell:AppsFolder\\{a['AppID']}") for a in apps if a.get("Name") and a.get("AppID")]
    except (OSError, ValueError, subprocess.SubprocessError):
        found = []
    if not found:  # no PowerShell: use the Start menu's shortcut files
        for base in (os.environ.get("APPDATA"), os.environ.get("PROGRAMDATA")):
            folder = Path(base or "") / "Microsoft/Windows/Start Menu/Programs"
            if base and folder.exists():
                found += [(p.stem, str(p)) for p in folder.rglob("*.lnk")]
    return [(name, target) for name, target in found if not NOT_APP_RE.search(name)]


def start_apps(max_age: float = 600):
    with _apps_lock:
        if WINDOWS and (not _apps["list"] or time.time() - _apps["at"] > max_age):
            found = _read_start_apps()
            if found:
                _apps.update(list=found, at=time.time())
        return _apps["list"]


def _find_app(name: str, fuzzy: bool):
    """(display name, target) of the Start menu app called `name`, or None."""
    n = _norm(name)
    n = APP_ALIASES.get(n, n)
    for refresh in (False, True):
        # A miss rereads the Start menu (at most once a minute), so newly installed apps are found.
        apps = start_apps(max_age=60 if refresh else 600)
        named = [(_norm(a[0]), a) for a in apps]
        for key, app in named:
            if key == n or key.replace(" ", "") == n.replace(" ", ""):
                return app
        if fuzzy:
            words = n.split()
            hits = [(len(key), app) for key, app in named
                    if all(w in key.split() or (len(w) >= 4 and any(k.startswith(w) for k in key.split())) for w in words)]
            if hits:
                return min(hits)[1]
            import difflib
            close = difflib.get_close_matches(n, [key for key, _ in named], n=1, cutoff=0.82)
            if close:
                return next(app for key, app in named if key == close[0])
        if not refresh and WINDOWS and time.time() - _apps["at"] < 60:
            break
    return None


def _strip_target(target: str) -> str:
    t = re.sub(r"^(?:up|the|my|a|an)\s+", "", target.strip())
    return re.sub(r"\s+(?:app|application|program|website|web ?site|site|page|window|on my (?:pc|computer|laptop))$", "", t).strip()


def _resolve_open(target: str, fuzzy: bool = True):
    """An "open" action for one thing, or None if Nexa doesn't know it."""
    t = _norm(_strip_target(target))
    if not t:
        return None
    if app := _find_app(t, fuzzy=False):
        return {"do": "open", "label": app[0], "target": app[1], "app": True}
    settings = re.fullmatch(r"(.+?) settings?", t)
    if settings and settings.group(1) in SETTINGS_PAGES:
        return {"do": "open", "label": f"{settings.group(1)} settings", "target": SETTINGS_PAGES[settings.group(1)]}
    if t in FOLDERS:
        return {"do": "open", "label": FOLDERS[t][0], "target": FOLDERS[t][1]}
    if t in SITES:
        return {"do": "open", "label": SITES[t][0], "target": SITES[t][1]}
    if t in SETTINGS_PAGES:
        return {"do": "open", "label": f"{t} settings", "target": SETTINGS_PAGES[t]}
    domain = re.sub(r"\s+dot\s+", ".", _strip_target(target).lower()).replace(" ", "")
    if re.fullmatch(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?", domain):
        return {"do": "open", "label": re.sub(r"^https?://", "", domain), "target": domain if "://" in domain else f"https://{domain}"}
    if fuzzy and (app := _find_app(t, fuzzy=True)):
        return {"do": "open", "label": app[0], "target": app[1], "app": True}
    return None


def _plan_one(c: str):
    if not c:
        return None
    for key, pattern in MEDIA:
        if pattern.match(c):
            steps = 10 if c.endswith("a lot") else 5
            return {"do": "key", "key": key, "times": steps if key in ("up", "down") else 1, "unmute": c.startswith("unmute")}
    m = VOLUME_SET_RE.match(c)
    if m:
        amount = "max" if re.match(r"(?:max|full) volume", c) else m.group(1).strip()
        level = int(amount) if amount.isdigit() else NUMBER_WORDS.get(amount)
        if level is not None and 0 <= level <= 100:
            return {"do": "volume", "level": level}
    if LOCK_RE.match(c):
        return {"do": "lock"}
    if SNIP_RE.match(c):
        return {"do": "open", "label": "the snipping tool", "target": "ms-screenclip:", "say": "Pick the part of the screen to capture."}
    if DESKTOP_RE.match(c):
        return {"do": "desktop"}
    if POWER_RE.match(c):
        return {"do": "refuse"}
    if NOW_PLAYING_RE.match(c):
        return {"do": "now_playing"}
    m = PLATFORM_RE.match(c)
    if m or (m := PLATFORM_FIRST_RE.match(c)):
        query, where = (m.group(1), m.group(2)) if m.re is PLATFORM_RE else (m.group(2), m.group(1))
        query = re.sub(r"^(?:some|the song|the album|the playlist|a song called|songs by|music by|videos? (?:of|about))\s+", "", query)
        return {"do": "find", "where": "spotify" if where == "spotify" else "youtube", "query": query,
                "play": c.startswith(("play", "put on", "listen to", "watch"))}
    m = PLAY_ANY_RE.match(c)
    if m and not NOT_SONG_RE.search(m.group(1)):
        query = re.sub(r"^(?:some|the song|the album|the playlist|a song called|songs by|music by)\s+", "", m.group(1))
        where = "spotify" if _find_app("spotify", fuzzy=False) else "youtube"
        return {"do": "find", "where": where, "query": query, "play": True}
    m = CLOSE_RE.match(c)
    if m:
        t = _norm(_strip_target(m.group(1)))
        if t and t not in NOT_CLOSABLE:
            name = APP_ALIASES.get(t, t)
            app = _find_app(name, fuzzy=False)
            if app or _matching_windows(name):
                return {"do": "close", "name": name, "label": app[0] if app else t.title()}
        return None
    m = OPEN_RE.match(c)
    if m:
        verb, target = m.groups()
        # "Start"/"run"/"go to" are everyday words too ("start the game"), so they only open exact names.
        fuzzy = verb in ("open", "launch", "fire up", "bring up", "pull up")
        whole = _resolve_open(target, fuzzy)
        if whole:
            return whole
        parts = [p for p in re.split(r"\s*(?:,|\band\b|&)\s*", target) if p.strip()]
        if len(parts) > 1:
            each = [_resolve_open(p, fuzzy) for p in parts]
            if all(each):
                return {"do": "many", "steps": each}
        name = _strip_target(target)
        chat_words = r"\b(?:is|are|was|were|be|what|why|how|who|when|does|do|did|can|should|would|about|means?|vs|or)\b"
        if verb in ("open", "launch") and len(name.split()) <= 3 and not re.search(chat_words, name):
            return {"do": "missing", "label": name}
    return None


def plan_action(text: str):
    """The PC command in a message, or None to let the AI answer it."""
    if not WINDOWS or not text or len(text) > 200:
        return None
    c = _clean_command(text)
    # "Open Spotify and play lofi beats": several commands in one sentence.
    verbs = r"open|launch|start|close|quit|play|pause|skip|turn|set|mute|unmute|search|lock|take|show|go to|put on"
    clauses = re.split(rf",?\s+(?:and then|then|and also|and)\s+(?=(?:{verbs})\b)", c)
    if len(clauses) > 1:
        steps = [_plan_one(_clean_command(clause)) for clause in clauses]
        if all(steps) and not any(s["do"] == "missing" for s in steps):
            return {"do": "many", "steps": steps}
    return _plan_one(c)


def _press(vk: int, times: int = 1):
    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(vk, 0, 1, 0)  # KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(vk, 0, 3, 0)  # ... | KEYEVENTF_KEYUP


def _windows():
    """Visible top-level app windows as (hwnd, title, exe name, window class)."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        h = ctypes.c_void_p(hwnd)
        length = user32.GetWindowTextLengthW(h)
        if not length or not user32.IsWindowVisible(h) or user32.GetWindow(h, 4):  # 4 = GW_OWNER
            return True
        if user32.GetWindowLongW(h, -20) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(h, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe = ""
        process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if process:
            path, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(process), 0, path, ctypes.byref(size)):
                exe = Path(path.value).stem.lower()
            kernel32.CloseHandle(ctypes.c_void_p(process))
        found.append((hwnd, title.value, exe, cls.value, pid.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def _matching_windows(name: str):
    """Windows of the app called `name`: by its program file, or a title ending in " - Name"."""
    exe = CLOSE_EXES.get(name, name).replace(" ", "")
    hits = []
    for hwnd, title, proc, cls, pid in _windows():
        if title.startswith("Nexa") or pid == os.getpid():
            continue  # never Nexa's own window or console
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            continue  # the desktop and taskbar
        if proc == "explorer" and cls != "CabinetWClass":
            continue  # only File Explorer's folder windows
        last = _norm(re.split(r" [-–—] ", title)[-1])
        if proc.replace(" ", "") == exe or last == name or _norm(title) == name:
            hits.append((hwnd, title, proc))
    return hits


def _bring_forward(name: str, seconds: float = 8.0):
    """Windows keeps apps opened by a background program (Nexa) behind the current window,
    and an app that's already running just stays where it is. Bring its window up."""
    user32 = ctypes.windll.user32
    end = time.time() + seconds
    while time.time() < end:
        windows = _matching_windows(name)
        if windows:
            hwnd = ctypes.c_void_p(windows[0][0])
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.keybd_event(0x12, 0, 0, 0)  # an Alt tap lets this program switch windows
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            return
        time.sleep(0.25)


def _now_playing():
    for _, title, proc in _matching_windows("spotify"):
        if proc == "spotify" and " - " in title:
            artist, song = title.split(" - ", 1)
            return f"This is {song} by {artist}."
    return "I can only tell what's playing in Spotify, and it isn't playing anything right now."


def _youtube_first(query: str):
    """The first video's address for a YouTube search, so "play X on YouTube" starts playing."""
    try:
        r = httpx.get("https://www.youtube.com/results", params={"search_query": query}, timeout=6,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36",
                               "Accept-Language": "en-US,en;q=0.9"})
        m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', r.text)
        return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None
    except httpx.HTTPError:
        return None


def _run_one(plan: dict) -> str:
    do = plan["do"]
    if do == "open":
        os.startfile(plan["target"])
        if plan.get("app"):
            name = APP_ALIASES.get(_norm(plan["label"]), _norm(plan["label"]))
            threading.Thread(target=_bring_forward, args=[name], daemon=True).start()
        return plan.get("say") or f"Opening {plan['label']}."
    if do == "missing":
        return f"Hmm, I couldn't find {plan['label']} on this PC."
    if do == "key":
        key = plan["key"]
        _press({"play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1, "up": 0xAF, "down": 0xAE, "mute": 0xAD}[key],
               plan["times"])
        return {"play": "Playing.", "pause": "Paused.", "next": "Next song.", "previous": "Previous song.",
                "up": "Turning it up.", "down": "Turning it down.",
                "mute": "Sound's back on." if plan["unmute"] else "Muted."}[key]
    if do == "volume":
        _press(0xAE, 50)  # each key step is 2%: all the way down, then up to the level
        _press(0xAF, round(plan["level"] / 2))
        return f"Volume set to {plan['level']} percent."
    if do == "find":
        query = plan["query"]
        if plan["where"] == "spotify":
            try:
                os.startfile(f"spotify:search:{quote(query)}")
            except OSError:
                os.startfile(f"https://open.spotify.com/search/{quote(query)}")
            return f"Here's {query} on Spotify."
        video = _youtube_first(query) if plan["play"] else None
        os.startfile(video or f"https://www.youtube.com/results?search_query={quote(query)}")
        return f"Playing {query} on YouTube." if video else f"Here's {query} on YouTube."
    if do == "close":
        windows = _matching_windows(plan["name"])
        for hwnd, _, _ in windows:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)  # WM_CLOSE, like its X button
        return f"Closing {plan['label']}." if windows else f"{plan['label']} isn't open."
    if do == "lock":
        threading.Timer(2.0, ctypes.windll.user32.LockWorkStation).start()  # after she's said it
        return "Locking the PC."
    if do == "desktop":
        user32 = ctypes.windll.user32
        user32.keybd_event(0x5B, 0, 0, 0)  # Windows key + D
        user32.keybd_event(0x44, 0, 0, 0)
        user32.keybd_event(0x44, 0, 2, 0)
        user32.keybd_event(0x5B, 0, 2, 0)
        return "Here's your desktop."
    if do == "now_playing":
        return _now_playing()
    if do == "refuse":
        return "I can't shut down or restart the PC. That one's up to you."
    return ""


def run_action(plan: dict, address: str = "") -> str:
    """Carry out a planned command and return what Nexa says about it."""
    said = []
    for step in plan["steps"] if plan["do"] == "many" else [plan]:
        try:
            said.append(_run_one(step))
        except OSError as e:
            log(f"command {step['do']} failed: {e}")
            said.append(f"I couldn't open {step.get('label') or 'that'}.")
    opens = [s[len("Opening "):-1] for s in said if s.startswith("Opening ")]
    if len(opens) > 1:  # "Opening Notepad and Calculator."
        said = [f"Opening {', '.join(opens[:-1])} and {opens[-1]}."] + [s for s in said if not s.startswith("Opening ")]
    if address and said:
        said[0] = f"{said[0][:-1]}, {address}{said[0][-1]}"
    return " ".join(said)


# ---------- Gemini API (optional online brain, free tier) ----------
#
# The key is stored in the user's profile, not the project folder, so copying or
# sharing the project never shares the key. GEMINI_API_KEY overrides the file.

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
# The assistant was first called Ciel, and its data folder had that name. Move it once so saved
# keys, chats, settings and the microphone permission carry over.
NEXA_HOME = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "Nexa"
_OLD_HOME = NEXA_HOME.with_name("Ciel")
if _OLD_HOME.is_dir() and not NEXA_HOME.exists():
    try:
        _OLD_HOME.rename(NEXA_HOME)
    except OSError:  # in use (her window is still open): keep using it and move it next time
        NEXA_HOME = _OLD_HOME
GEMINI_KEY_PATH = NEXA_HOME / "gemini_api_key.txt"
owner_voice = OwnerVoice(NEXA_HOME, VOICES_DIR / "speaker")
GEMINI_MODELS_PATH = NEXA_HOME / "gemini_models.json"  # last known list, for when offline
GEMINI_SKIP_RE = re.compile(r"embedding|image|tts|audio|live|robotics|computer-use|aqa|learnlm", re.I)


class BrainError(Exception):
    """An online brain (Groq, Gemini or Claude) answered with an HTTP error."""

    def __init__(self, status: int, detail: str, retry_after: float = 0):
        super().__init__(f"{status}: {detail[:200]}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


# Each online brain's name and company, for messages.
BRAINS = {"groq": ("Groq", "Groq"), "gemini": ("Gemini", "Google"), "claude": ("Claude", "Anthropic")}

# After a "limit reached" answer, that brain isn't asked again until this time.
_rest = {p: {"until": 0.0, "reason": ""} for p in BRAINS}


def limit_wait(e: BrainError) -> float:
    """How long to leave a brain alone after "limit reached": the service's suggested wait, at
    least a minute, and at least an hour for daily limits."""
    seconds = max(60.0, e.retry_after)
    try:
        err = json.loads(e.detail).get("error", {})
        for d in err.get("details", []) if isinstance(err, dict) else []:
            if isinstance(d, dict) and str(d.get("retryDelay", "")).endswith("s"):
                seconds = max(seconds, float(d["retryDelay"][:-1]))
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    if re.search(r"per ?day|perday|daily|\(TPD\)|\(RPD\)", e.detail, re.I):
        seconds = max(seconds, 3600)
    return seconds


def rest_brain(provider: str, e: BrainError):
    """Remember a limit (see limit_wait); an hour when Claude's prepaid credit runs out."""
    seconds = limit_wait(e)
    what = "Claude's rate limit was reached" if provider == "claude" else f"{BRAINS[provider][0]}'s free limit was reached"
    if provider == "claude" and "credit" in e.detail.lower():
        seconds, what = 3600, "Claude's credit balance is too low"
    until = time.time() + seconds
    _rest[provider].update(until=until, reason=f"{what}, back at {datetime.fromtimestamp(until):%I:%M %p}")
    log(f"{provider} resting for {seconds:.0f}s ({_rest[provider]['reason']})")


async def auto_gemini_models(voice: bool) -> list[str]:
    """Models Auto mode tries in order (newest first).

    Spoken requests start with Flash-Lite (first words in ~1 s); typed ones start
    with Flash, which thinks longer (~10 s) but answers harder questions better.
    The other kind is the backup when the first is busy.
    """
    models = cached_gemini_models()
    if not models and gemini_key():
        try:
            models = await fetch_gemini_models(gemini_key())
        except httpx.HTTPError:
            pass
    ids = [m["id"] for m in models if "preview" not in m["id"] and "latest" not in m["id"]] or [m["id"] for m in models]
    lite = [i for i in ids if "flash-lite" in i]
    flash = [i for i in ids if "flash" in i and "lite" not in i]
    order = lite[:2] + flash[:1] if voice else flash[:2] + lite[:1]
    return order or ids[:3]


def gemini_label(model_id: str) -> str:
    return next((m["label"] for m in cached_gemini_models() if m["id"] == model_id), model_id)


def gemini_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY") or getattr(local_config, "GEMINI_API_KEY", "")).strip()
    if key:
        return key
    try:
        return GEMINI_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _gemini_sort_key(model: dict):
    """Flash models first (they're on the free tier), full before Lite, newest version first."""
    mid = model["id"]
    version = re.search(r"gemini-(\d+(?:\.\d+)?)", mid)
    return ("flash" not in mid, "lite" in mid, -float(version.group(1)) if version else 0, mid)


async def fetch_gemini_models(key: str) -> list[dict]:
    """Asks Google which models this key may use for chat. Raises httpx errors."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GEMINI_API}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": key})
    r.raise_for_status()
    models = []
    for m in r.json().get("models", []):
        mid = m.get("name", "").removeprefix("models/")
        if (
            mid.startswith("gemini")
            and "generateContent" in m.get("supportedGenerationMethods", [])
            and not GEMINI_SKIP_RE.search(mid)
        ):
            models.append({"id": mid, "label": m.get("displayName") or mid})
    models.sort(key=_gemini_sort_key)
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    GEMINI_MODELS_PATH.write_text(json.dumps(models), encoding="utf-8")
    return models


def cached_gemini_models() -> list[dict]:
    try:
        return json.loads(GEMINI_MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


_no_thinking_level = set()  # models that rejected thinkingLevel


async def stream_gemini(model_id: str, messages: list[dict]):
    """Streams reply text from Gemini. Raises BrainError or httpx.HTTPError."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages
        if m["role"] != "system"
    ]
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}
    version = re.search(r"gemini-(\d+)", model_id)
    if version and int(version.group(1)) >= 3 and model_id not in _no_thinking_level:
        # Gemini 3+ thinks before answering ("medium" by default); "low" answers sooner.
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": "low"}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=8)) as client:
        async with client.stream(
            "POST",
            f"{GEMINI_API}/models/{model_id}:streamGenerateContent",
            params={"alt": "sse"},
            headers={"x-goog-api-key": gemini_key()},
            json=body,
        ) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")
                if r.status_code == 400 and "generationConfig" in body and "think" in detail.lower():
                    _no_thinking_level.add(model_id)  # this model doesn't take the setting; ask again without it
                    log(f"gemini {model_id}: thinkingLevel not accepted, retrying without it")
                    async for text in stream_gemini(model_id, messages):
                        yield text
                    return
                raise BrainError(r.status_code, detail)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:])
                for candidate in data.get("candidates", [])[:1]:
                    for part in candidate.get("content", {}).get("parts", []):
                        if part.get("text") and not part.get("thought"):
                            yield part["text"]


def describe_problem(provider: str, e: Exception) -> str:
    name, company = BRAINS[provider]
    if isinstance(e, BrainError):
        if e.status == 429:
            return f"{name}'s limit is used up"
        if e.status in (400, 401, 403):
            return f"{company} rejected the request or the API key"
        if e.status == 404:
            return f"that {name} model isn't available"
        if e.status in (500, 503, 529):
            return f"{name} is busy right now"
        return f"{company} returned error {e.status}"
    return f"{name} couldn't be reached"


# ---------- Claude API (optional online brain, paid: no free tier) ----------

ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01"}
CLAUDE_KEY_PATH = NEXA_HOME / "anthropic_api_key.txt"
CLAUDE_MODELS_PATH = NEXA_HOME / "claude_models.json"


def claude_key() -> str:
    key = (os.environ.get("ANTHROPIC_API_KEY") or getattr(local_config, "CLAUDE_API_KEY", "")).strip()
    if key:
        return key
    try:
        return CLAUDE_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def fetch_claude_models(key: str) -> list[dict]:
    """Asks Anthropic which models this key may use (newest first). Raises httpx errors."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ANTHROPIC_API}/models", params={"limit": 100},
                             headers={**ANTHROPIC_HEADERS, "x-api-key": key})
    r.raise_for_status()
    models = [{"id": m["id"], "label": m.get("display_name") or m["id"]}
              for m in r.json().get("data", []) if m.get("id", "").startswith("claude")]
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    CLAUDE_MODELS_PATH.write_text(json.dumps(models), encoding="utf-8")
    return models


def cached_claude_models() -> list[dict]:
    try:
        return json.loads(CLAUDE_MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


async def auto_claude_models(voice: bool) -> list[str]:
    """Spoken requests start with Haiku (fastest); typed ones with Sonnet (smarter)."""
    models = cached_claude_models()
    if not models and claude_key():
        try:
            models = await fetch_claude_models(claude_key())
        except httpx.HTTPError:
            pass
    ids = [m["id"] for m in models]
    haiku = [i for i in ids if "haiku" in i][:1]
    sonnet = [i for i in ids if "sonnet" in i][:1]
    order = haiku + sonnet if voice else sonnet + haiku
    return order or ids[:2]


def claude_label(model_id: str) -> str:
    return next((m["label"] for m in cached_claude_models() if m["id"] == model_id), model_id)


async def stream_claude(model_id: str, messages: list[dict], voice: bool = False):
    """Streams reply text from Claude (Messages API). Raises BrainError or httpx.HTTPError."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = []
    for m in messages:
        if m["role"] == "system":
            continue
        if turns and turns[-1]["role"] == m["role"]:
            turns[-1]["content"] += "\n\n" + m["content"]  # Claude needs alternating turns
        else:
            turns.append({"role": m["role"], "content": m["content"]})
    body = {"model": model_id, "max_tokens": 1024 if voice else 4096, "system": system,
            "messages": turns, "stream": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=8)) as client:
        async with client.stream("POST", f"{ANTHROPIC_API}/messages", json=body,
                                 headers={**ANTHROPIC_HEADERS, "x-api-key": claude_key()}) as r:
            if r.status_code != 200:
                retry = float(r.headers.get("retry-after", 0) or 0)
                raise BrainError(r.status_code, (await r.aread()).decode(errors="replace"), retry)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:])
                if data.get("type") == "content_block_delta" and data["delta"].get("type") == "text_delta":
                    yield data["delta"]["text"]
                elif data.get("type") == "error":
                    kind = data.get("error", {}).get("type", "")
                    raise BrainError(529 if kind == "overloaded_error" else 500, json.dumps(data))


# ---------- Groq API (optional online brain, free tier: very fast, keeps nothing by default) ----------

GROQ_API = "https://api.groq.com/openai/v1"
GROQ_KEY_PATH = NEXA_HOME / "groq_api_key.txt"
GROQ_MODELS_PATH = NEXA_HOME / "groq_models.json"
# Smartest first; then every other chat model the key has. Each model has its own free daily
# allowance, so when one runs out Nexa moves down the list. Spoken requests start with the
# 20B model instead: about a second quicker, and plenty for short spoken answers.
GROQ_PREFERRED = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
GROQ_VOICE_FIRST = "openai/gpt-oss-20b"
GROQ_LABELS = {"openai/gpt-oss-120b": "GPT-OSS 120B", "openai/gpt-oss-20b": "GPT-OSS 20B"}
GROQ_SKIP_RE = re.compile(r"whisper|tts|guard|embed|orpheus|playai|compound|allam", re.I)  # not chat models
_groq_rest: dict[str, float] = {}  # model -> when its free limit lets it answer again
_no_reasoning_effort = set()  # models that rejected reasoning_effort


def groq_key() -> str:
    key = (os.environ.get("GROQ_API_KEY") or getattr(local_config, "GROQ_API_KEY", "")).strip()
    if key:
        return key
    try:
        return GROQ_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def fetch_groq_models(key: str) -> list[dict]:
    """Asks Groq which chat models this key may use, best first. Raises httpx errors."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GROQ_API}/models", headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])
           if m.get("id") and m.get("active", True) and not GROQ_SKIP_RE.search(m["id"])]
    ids.sort(key=lambda i: (GROQ_PREFERRED.index(i) if i in GROQ_PREFERRED else len(GROQ_PREFERRED), i))
    models = [{"id": i, "label": GROQ_LABELS.get(i, i.split("/")[-1])} for i in ids]
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    GROQ_MODELS_PATH.write_text(json.dumps(models), encoding="utf-8")
    return models


def cached_groq_models() -> list[dict]:
    try:
        return json.loads(GROQ_MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


async def auto_groq_models(voice: bool) -> list[str]:
    """The models this key has, in the order to try them, skipping any whose free limit is used up."""
    models = cached_groq_models()
    if not models and groq_key():
        try:
            models = await fetch_groq_models(groq_key())
        except httpx.HTTPError:
            pass
    ids = [m["id"] for m in models]  # already smartest first
    if voice and GROQ_VOICE_FIRST in ids:
        ids = [GROQ_VOICE_FIRST] + [i for i in ids if i != GROQ_VOICE_FIRST]
    return [i for i in ids if time.time() >= _groq_rest.get(i, 0)]


def groq_label(model_id: str) -> str:
    return next((m["label"] for m in cached_groq_models() if m["id"] == model_id), model_id)


def groq_resting() -> str:
    """Why Groq can't answer right now (every model it would try is over its limit), or ""."""
    ids = [m["id"] for m in cached_groq_models()]
    back = min((_groq_rest.get(i, 0) for i in ids), default=0)
    return f"Groq's free limits were reached, back at {datetime.fromtimestamp(back):%I:%M %p}" if back > time.time() else ""


async def stream_groq(model_id: str, messages: list[dict], voice: bool = False, max_tokens: int = 0):
    """Streams reply text from Groq (OpenAI-style chat). Raises BrainError or httpx.HTTPError."""
    max_tokens = max_tokens or (1024 if voice else 4096)
    body = {"model": model_id, "messages": messages, "stream": True, "max_completion_tokens": max_tokens}
    if "gpt-oss" in model_id and model_id not in _no_reasoning_effort:
        # GPT-OSS thinks before answering; "low" answers sooner and uses less of the daily allowance.
        body["reasoning_effort"] = "low"
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=8)) as client:
        async with client.stream("POST", f"{GROQ_API}/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {groq_key()}"}) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")
                if r.status_code == 400 and "reasoning_effort" in body and "reasoning" in detail.lower():
                    _no_reasoning_effort.add(model_id)  # this model doesn't take the setting; ask again without it
                    async for text in stream_groq(model_id, messages, voice, max_tokens):
                        yield text
                    return
                # Some free models cap the reply size per minute ("Request too large ... Limit 1000"):
                # ask once more for a reply that fits.
                cap = re.search(r"Request too large.*?Limit (\d+)", detail)
                if r.status_code == 429 and cap and int(cap.group(1)) > 200 and max_tokens > int(cap.group(1)) - 100:
                    async for text in stream_groq(model_id, messages, voice, int(cap.group(1)) - 100):
                        yield text
                    return
                raise BrainError(r.status_code, detail, float(r.headers.get("retry-after", 0) or 0))
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                data = json.loads(payload)
                if data.get("error"):
                    raise BrainError(500, payload)
                for choice in data.get("choices", [])[:1]:
                    if (choice.get("delta") or {}).get("content"):
                        yield choice["delta"]["content"]


@asynccontextmanager
async def lifespan(_app):
    # Load every model in the background so the first message is fast.
    def load_hearing():
        get_whisper(WHISPER_MODEL)
        get_whisper(WHISPER_ACCURATE_MODEL)

    threading.Thread(target=load_hearing, daemon=True).start()
    threading.Thread(target=warm_model, daemon=True).start()
    threading.Thread(target=start_apps, daemon=True).start()  # so "open Spotify" is instant
    if VOICE_PATH.exists():
        threading.Thread(target=get_piper, daemon=True).start()
    yield


app = FastAPI(title="Nexa", lifespan=lifespan)
class FreshStaticFiles(StaticFiles):
    """Static files the browser re-checks every time, so an updated Nexa never runs old scripts."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", FreshStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = sorted(m["name"] for m in r.json().get("models", []))
        ollama_ok = True
    except Exception:
        models, ollama_ok = [], False
    return {
        "ollama": ollama_ok,
        "models": models,
        "default_model": DEFAULT_MODEL,
        "model_ready": _model_ready,
        "speech_ready": WHISPER_MODEL in _whispers,
        "accurate_hearing": WHISPER_ACCURATE_MODEL in _whispers,
        "hearing": {name: _stt_device.get(name) for name in (WHISPER_MODEL, WHISPER_ACCURATE_MODEL)},
        "tts_ready": bool(installed_voices()),
        "owner": owner_voice.status(),
        "online": await check_online(),
        "gemini": bool(gemini_key()),
        "gemini_resting": _rest["gemini"]["reason"] if time.time() < _rest["gemini"]["until"] else "",
        "claude": bool(claude_key()),
        "claude_resting": _rest["claude"]["reason"] if time.time() < _rest["claude"]["until"] else "",
        "groq": bool(groq_key()),
        "groq_resting": groq_resting(),
    }


@app.get("/api/gemini")
async def gemini_info():
    """Whether a key is saved, and the Gemini models it may use (last known list when offline)."""
    key = gemini_key()
    if not key:
        return {"configured": False, "models": []}
    try:
        return {"configured": True, "models": await fetch_gemini_models(key)}
    except httpx.HTTPStatusError as e:
        rejected = e.response.status_code in (400, 401, 403)
        return {"configured": True, "models": cached_gemini_models(),
                "error": "Google rejected the saved key" if rejected else f"Google returned error {e.response.status_code}"}
    except httpx.HTTPError:
        return {"configured": True, "models": cached_gemini_models(), "error": "offline"}


@app.post("/api/gemini/key")
async def gemini_save_key(req: Request):
    """Body: {"key": "..."}. Checks the key with Google before saving it. The key is never logged."""
    key = ((await req.json()).get("key") or "").strip().strip("'\"").strip()
    # Google has used several key formats, so only rule out obvious mistakes and let Google decide.
    if not re.fullmatch(r"[\x21-\x7e]{20,300}", key):
        log(f"gemini key not saved: doesn't look like a key ({len(key)} characters, or contains spaces)")
        return JSONResponse({"error": "That doesn't look like an API key. Copy it again from AI Studio."}, status_code=400)
    try:
        models = await fetch_gemini_models(key)
    except httpx.HTTPStatusError as e:
        try:
            reason = e.response.json()["error"]["message"][:200]
        except (ValueError, KeyError, TypeError):
            reason = f"error {e.response.status_code}"
        log(f"gemini key not saved: Google answered {e.response.status_code}: {reason}")
        return JSONResponse({"error": f"Google rejected that key: {reason}"}, status_code=400)
    except httpx.HTTPError as e:
        log(f"gemini key not saved: couldn't reach Google ({type(e).__name__})")
        return JSONResponse({"error": "Couldn't reach Google. Connect to the internet and try again."}, status_code=503)
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    GEMINI_KEY_PATH.write_text(key, encoding="utf-8")
    log(f"gemini key saved ({len(models)} models available)")
    return {"ok": True, "models": models}


@app.delete("/api/gemini/key")
def gemini_remove_key():
    GEMINI_KEY_PATH.unlink(missing_ok=True)
    GEMINI_MODELS_PATH.unlink(missing_ok=True)
    log("gemini key removed")
    return {"ok": True}


@app.get("/api/claude")
async def claude_info():
    """Whether a Claude key is saved, and the models it may use (last known list when offline)."""
    key = claude_key()
    if not key:
        return {"configured": False, "models": []}
    try:
        return {"configured": True, "models": await fetch_claude_models(key)}
    except httpx.HTTPStatusError as e:
        rejected = e.response.status_code in (400, 401, 403)
        return {"configured": True, "models": cached_claude_models(),
                "error": "Anthropic rejected the saved key" if rejected else f"Anthropic returned error {e.response.status_code}"}
    except httpx.HTTPError:
        return {"configured": True, "models": cached_claude_models(), "error": "offline"}


@app.post("/api/claude/key")
async def claude_save_key(req: Request):
    """Body: {"key": "..."}. Checks the key with Anthropic before saving it. The key is never logged."""
    key = ((await req.json()).get("key") or "").strip().strip("'\"").strip()
    if not re.fullmatch(r"[\x21-\x7e]{20,300}", key):
        log(f"claude key not saved: doesn't look like a key ({len(key)} characters, or contains spaces)")
        return JSONResponse({"error": "That doesn't look like an API key. Copy it again from the Claude Console."}, status_code=400)
    try:
        models = await fetch_claude_models(key)
    except httpx.HTTPStatusError as e:
        try:
            reason = e.response.json()["error"]["message"][:200]
        except (ValueError, KeyError, TypeError):
            reason = f"error {e.response.status_code}"
        log(f"claude key not saved: Anthropic answered {e.response.status_code}: {reason}")
        return JSONResponse({"error": f"Anthropic rejected that key: {reason}"}, status_code=400)
    except httpx.HTTPError as e:
        log(f"claude key not saved: couldn't reach Anthropic ({type(e).__name__})")
        return JSONResponse({"error": "Couldn't reach Anthropic. Connect to the internet and try again."}, status_code=503)
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    CLAUDE_KEY_PATH.write_text(key, encoding="utf-8")
    log(f"claude key saved ({len(models)} models available)")
    return {"ok": True, "models": models}


@app.delete("/api/claude/key")
def claude_remove_key():
    CLAUDE_KEY_PATH.unlink(missing_ok=True)
    CLAUDE_MODELS_PATH.unlink(missing_ok=True)
    log("claude key removed")
    return {"ok": True}


@app.get("/api/groq")
async def groq_info():
    """Whether a Groq key is saved, and the models it may use (last known list when offline)."""
    key = groq_key()
    if not key:
        return {"configured": False, "models": []}
    try:
        return {"configured": True, "models": await fetch_groq_models(key)}
    except httpx.HTTPStatusError as e:
        rejected = e.response.status_code in (400, 401, 403)
        return {"configured": True, "models": cached_groq_models(),
                "error": "Groq rejected the saved key" if rejected else f"Groq returned error {e.response.status_code}"}
    except httpx.HTTPError:
        return {"configured": True, "models": cached_groq_models(), "error": "offline"}


@app.post("/api/groq/key")
async def groq_save_key(req: Request):
    """Body: {"key": "..."}. Checks the key with Groq before saving it. The key is never logged."""
    key = ((await req.json()).get("key") or "").strip().strip("'\"").strip()
    if not re.fullmatch(r"[\x21-\x7e]{20,300}", key):
        log(f"groq key not saved: doesn't look like a key ({len(key)} characters, or contains spaces)")
        return JSONResponse({"error": "That doesn't look like an API key. Copy it again from console.groq.com."}, status_code=400)
    try:
        models = await fetch_groq_models(key)
    except httpx.HTTPStatusError as e:
        try:
            reason = e.response.json()["error"]["message"][:200]
        except (ValueError, KeyError, TypeError):
            reason = f"error {e.response.status_code}"
        log(f"groq key not saved: Groq answered {e.response.status_code}: {reason}")
        return JSONResponse({"error": f"Groq rejected that key: {reason}"}, status_code=400)
    except httpx.HTTPError as e:
        log(f"groq key not saved: couldn't reach Groq ({type(e).__name__})")
        return JSONResponse({"error": "Couldn't reach Groq. Connect to the internet and try again."}, status_code=503)
    NEXA_HOME.mkdir(parents=True, exist_ok=True)
    GROQ_KEY_PATH.write_text(key, encoding="utf-8")
    log(f"groq key saved ({len(models)} models available)")
    return {"ok": True, "models": models}


@app.delete("/api/groq/key")
def groq_remove_key():
    GROQ_KEY_PATH.unlink(missing_ok=True)
    GROQ_MODELS_PATH.unlink(missing_ok=True)
    log("groq key removed")
    return {"ok": True}


_installed = {"models": set(), "checked": -1e9}


async def installed_models(max_age: float = 60) -> set:
    """Names of the models in Ollama, refreshed at most once a minute."""
    if time.monotonic() - _installed["checked"] > max_age:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{OLLAMA_URL}/api/tags")
                r.raise_for_status()
                names = {m["name"] for m in r.json().get("models", [])}
            _installed["models"] = names | {n.removesuffix(":latest") for n in names}
        except (httpx.HTTPError, ValueError):
            pass
        _installed["checked"] = time.monotonic()
    return _installed["models"]


CODE_LIBRARIES = re.compile(r"\b(pygame|vpython|visual python)\b", re.I)
_code_examples = {"index": None, "loaded": False}


def code_example(question: str):
    """The closest tested pygame/VPython example from training/, or None.

    The training folder (examples checked by actually running them) isn't part of the
    installer, so on other PCs this simply finds nothing.
    """
    if not _code_examples["loaded"]:
        _code_examples["loaded"] = True
        try:
            sys.path.extend([str(ROOT / "training"), str(ROOT / "training" / "checker")])
            from retrieval import ExampleIndex
            _code_examples["index"] = ExampleIndex()
        except Exception as e:  # noqa: BLE001 - missing or broken examples must never break chat
            log(f"code examples not loaded: {e}")
    index = _code_examples["index"]
    return index.best(question) if index else None


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    model = body.get("model") or "auto"
    now = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
    # How she addresses the user: "Sir", "Ma'am", "Master", a name, or nothing.
    address = re.sub(r"[^\w .'-]", "", str(body.get("address") or ""))[:40].strip()
    address_rule = (
        f" Address the user as '{address}' (for example 'Yes, {address}.'), at most once per reply."
        if address else " Do not call the user by any title or name."
    )
    # On the maker's own PC (marked by a file in Nexa's data folder, which is never shared),
    # she knows the person she's talking with is the one who made her.
    maker_rule = f" The person you're talking with is {MAKER}, your maker." if (NEXA_HOME / "maker").exists() else ""
    system = f"{SYSTEM_PROMPT}{maker_rule}{address_rule} The current local date and time is {now}." + (
        DISPLAY_HINT if body.get("display") is True else VOICE_HINT if body.get("voice") else ""
    )
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in body.get("messages", [])
        if m.get("role") in ("user", "assistant")
    ]
    question = history[-1]["content"] if history and history[-1]["role"] == "user" else ""
    action = await run_in_threadpool(plan_action, question) if question else None
    plan = plan_search(question) if body.get("web", True) and question and not action else None

    async def stream():
        # NDJSON lines: {"t": text} | {"status": msg} | {"sources": [...]} | {"error": msg}
        if action:
            reply = await run_in_threadpool(run_action, action, address)
            log(f"command {action['do']}: {question!r} -> {reply!r}")
            yield json.dumps({"brain": "⚡ PC command"}) + "\n"
            yield json.dumps({"t": reply}) + "\n"
            return
        messages = [{"role": "system", "content": system}] + history
        if plan:
            if await check_online(max_age=0 if not _online["ok"] else 30):
                what = "the weather" if plan["kind"] == "weather" else f"“{plan['query']}”"
                yield json.dumps({"status": f"Searching the web for {what}"}) + "\n"
                sources, context = await run_in_threadpool(run_search, plan)
                if sources:
                    yield json.dumps({"sources": sources}) + "\n"
                if plan["kind"] == "weather":
                    note = (
                        f"{context}\n\nAnswer my question from this live weather report. Give the "
                        "place, the current temperature, conditions, and the chance of rain."
                    )
                else:
                    note = (
                        f"{context}\n\nAnswer my question using the search results above when "
                        "they are relevant. Summarize briefly in your own words (a few sentences "
                        "or a short list); do not copy the results. For facts taken from a "
                        "result, add its number like [1]. If the results don't answer the "
                        "question, say so."
                    )
            else:
                yield json.dumps({"status": "Offline: answering from memory"}) + "\n"
                note = (
                    "(You are offline right now, so you could not search the web. Briefly tell "
                    "the user you are offline, then answer from your own knowledge if you can.)"
                )
            messages[-1] = {"role": "user", "content": f"{question}\n\n---\n{note}"}

        # NDJSON also carries {"brain": "..."}: which model actually answered.
        local_model = model
        voice = bool(body.get("voice"))
        keys = {"groq": groq_key, "gemini": gemini_key, "claude": claude_key}
        if model == "auto" or model.startswith(tuple(f"{p}:" for p in BRAINS)):
            # Online brains when possible, in order; anything that goes wrong moves on to
            # the next one, and finally to the local model.
            local_model = DEFAULT_MODEL
            online = await check_online(max_age=0 if not _online["ok"] else 30)
            if model != "auto":
                provider, chosen = model.split(":", 1)
                candidates = [(provider, chosen)] if online else []
            else:
                # Provider priority lives in code, with no model picker in the app.
                order = list(dict.fromkeys(p for p in (*ONLINE_BRAIN_ORDER, *BRAINS) if p in BRAINS))
                prefer = body.get("prefer")
                if prefer in order:
                    order = [prefer] + [p for p in order if p != prefer]
                auto = {"groq": auto_groq_models, "gemini": auto_gemini_models, "claude": auto_claude_models}
                candidates = []
                for provider in order if online else []:
                    if keys[provider]():
                        candidates += [(provider, i) for i in await auto[provider](voice=voice)]
            candidates = [(p, i) for p, i in candidates if re.fullmatch(r"[\w./\-]+", i) and keys[p]()]
            problem = ""
            if not online:
                problem = "you're offline"
            elif not candidates:
                problem = "no API key is saved"
            skipped = set()  # providers that are resting, rate-limited or rejected the key
            for provider, model_id in [] if problem else candidates:
                if provider in skipped:
                    continue
                if time.time() < _rest[provider]["until"]:
                    problem = _rest[provider]["reason"]
                    skipped.add(provider)
                    continue
                label = {"groq": groq_label, "gemini": gemini_label, "claude": claude_label}[provider](model_id)
                replies = {"groq": lambda: stream_groq(model_id, messages, voice),
                           "gemini": lambda: stream_gemini(model_id, messages),
                           "claude": lambda: stream_claude(model_id, messages, voice)}
                answered = False
                try:
                    async for text in replies[provider]():
                        if not answered:
                            yield json.dumps({"brain": f"✦ {label}"}) + "\n"
                        answered = True
                        yield json.dumps({"t": text}) + "\n"
                    if answered:
                        return
                    problem = f"{label} returned no answer"
                except (BrainError, httpx.HTTPError, ValueError) as e:
                    log(f"{provider} {model_id}: {str(e)[:200]}")
                    status = e.status if isinstance(e, BrainError) else 0
                    if status == 429 and provider == "groq" and not answered:
                        # Each Groq model has its own free allowance: rest this one, try the next.
                        _groq_rest[model_id] = time.time() + limit_wait(e)
                        problem = f"Groq's free limit for {label} was reached"
                        continue
                    if status == 429 or (provider == "claude" and isinstance(e, BrainError) and "credit" in e.detail.lower()):
                        rest_brain(provider, e)
                        problem = _rest[provider]["reason"]
                    else:
                        problem = describe_problem(provider, e)
                    if answered:
                        yield json.dumps({"error": f"{label} stopped because {problem}."}) + "\n"
                        return
                    if status not in (500, 503, 529):
                        skipped.add(provider)  # only "busy" errors are worth another model of the same brain
            if problem != "no API key is saved":
                yield json.dumps({"status": f"{problem[0].upper()}{problem[1:]}: answering with Gemma on this PC"}) + "\n"

        # pygame/VPython questions go to the bigger code model when it's installed (not for voice:
        # spoken answers have no code, and it's too slow to talk to). A follow-up like "now add a
        # score" counts too when the last reply had such code in it.
        keep_alive = KEEP_ALIVE
        last_reply = next((m["content"] for m in reversed(history[:-1]) if m["role"] == "assistant"), "")
        # Also when the code model was picked in the menu, so it still gets the example and the note
        code_question = question and not voice and local_model in (DEFAULT_MODEL, CODE_MODEL) and (
            CODE_LIBRARIES.search(question)
            or ("```" in last_reply and CODE_LIBRARIES.search(last_reply) and len(question.split()) >= 4)
        )
        if code_question and CODE_MODEL in await installed_models():
            local_model, keep_alive = CODE_MODEL, CODE_KEEP_ALIVE
            # Small models like to claim they "tested" code; they can't run anything here
            messages[0] = {"role": "system", "content": messages[0]["content"] + (
                " You can't run code, so never say you tested it or that it runs without errors."
                " When fixing code, point out each bug you fix, and give the whole fixed program.")}
            example = await run_in_threadpool(code_example, question)
            log(f"code question -> {CODE_MODEL}, example: {example['where'] if example else 'none'}")
            if example:
                # A tested example as an earlier turn of the chat helps it use the right functions
                messages[-1:-1] = [{"role": "user", "content": example["prompt"]},
                                   {"role": "assistant", "content": example["answer"]}]
            yield json.dumps({"status": f"Writing code with {CODE_MODEL}. It's slower, so this can take a minute or two"}) + "\n"
        yield json.dumps({"brain": f"{local_model} · on this PC"}) + "\n"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5)) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": local_model,
                        "messages": messages,
                        "stream": True,
                        "keep_alive": keep_alive,
                    },
                ) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode(errors="replace")
                        try:
                            detail = json.loads(detail).get("error", detail)
                        except ValueError:
                            pass
                        yield json.dumps({"error": detail}) + "\n"
                        return
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        if "error" in data:
                            yield json.dumps({"error": data["error"]}) + "\n"
                            return
                        text = data.get("message", {}).get("content", "")
                        if text:
                            yield json.dumps({"t": text}) + "\n"
        except httpx.ConnectError:
            yield json.dumps(
                {"error": "Ollama isn't running. Open Ollama from the Start menu, then try again."}
            ) + "\n"

    events = display_reply_stream(stream()) if body.get("display") is True else stream()
    return StreamingResponse(events, media_type="application/x-ndjson")


_device_locks = {"cpu": threading.Lock(), "cuda": threading.Lock()}


def _local_owner_request(req):
    """Enrollment/reset are local settings, never actions from a remote web page."""
    return (
        req.client is not None and req.client.host in ("127.0.0.1", "::1")
        and req.headers.get("sec-fetch-site") not in ("cross-site", "same-site")
        and req.headers.get("origin", f"{req.url.scheme}://{req.url.netloc}") == f"{req.url.scheme}://{req.url.netloc}"
    )


@app.get("/api/owner")
def owner_info():
    return owner_voice.status()


@app.post("/api/owner/prepare")
async def owner_prepare(req: Request):
    if not _local_owner_request(req):
        return JSONResponse({"error": "Open Settings on this PC to set up your voice."}, status_code=403)
    try:
        await run_in_threadpool(owner_voice.prepare)
        return owner_voice.status()
    except Exception as e:
        log(f"owner voice setup failed: {type(e).__name__}")
        return JSONResponse({"error": "Speaker recognition isn't ready. Run setup.ps1 with internet, then restart Nexa."}, status_code=503)


@app.post("/api/owner/enroll")
async def owner_enroll(req: Request):
    if not _local_owner_request(req):
        return JSONResponse({"error": "Open Settings on this PC to enroll your voice."}, status_code=403)
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > 3 * 20 * SAMPLE_RATE * 2 * 4 // 3 + 1024:
            return JSONResponse({"error": "Voice samples are too long."}, status_code=413)
    try:
        samples = json.loads(raw).get("samples")
        if not isinstance(samples, list) or not 1 <= len(samples) <= 3:
            raise ValueError("Record one short voice sample.")
        recordings = []
        for sample in samples:
            pcm = base64.b64decode(sample, validate=True)
            if len(pcm) % 2 or not 2 * SAMPLE_RATE * 2 <= len(pcm) <= 20 * SAMPLE_RATE * 2:
                raise ValueError("Please record a sentence between 2 and 20 seconds long.")
            recordings.append(louder(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0))
        await run_in_threadpool(owner_voice.enroll, recordings)
        return owner_voice.status()
    except (ValueError, TypeError, AttributeError, binascii.Error) as e:
        return JSONResponse({"error": str(e) or "Invalid voice samples."}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Couldn't save your voice. Run setup.ps1 and try again."}, status_code=503)


@app.delete("/api/owner")
def owner_remove(req: Request):
    if not _local_owner_request(req):
        return JSONResponse({"error": "Open Settings on this PC to remove your voice."}, status_code=403)
    owner_voice.remove()
    return owner_voice.status()


def _speech_text(segments) -> str:
    """Reject a doubtful phrase as a whole rather than act on a partial PC command."""
    parts = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if (
            not np.isfinite(segment.avg_logprob) or segment.avg_logprob < -1.0
            or not np.isfinite(segment.compression_ratio) or segment.compression_ratio > 2.4
            or (getattr(segment, "no_speech_prob", 0) > 0.8 and segment.avg_logprob < -0.5)
        ):
            return ""
        # A confident sentence average can hide a guessed name, number or negation.
        # Keep every word or reject the entire turn; dropping one can change a command.
        words = getattr(segment, "words", None)
        if words is not None:
            probabilities = [w.probability for w in words if re.search(r"\w", w.word)]
            if not probabilities or not np.isfinite(probabilities).all():
                return ""
            if min(probabilities) < 0.15 or np.mean(probabilities) < 0.55:
                return ""
        parts.append(text)
    return " ".join(parts).strip()


def _transcribe(audio: np.ndarray, accurate: bool, hint: str | None = None) -> tuple[str, float, str]:
    """Returns (text, seconds waited for another transcription, "model/device")."""
    name = WHISPER_ACCURATE_MODEL if accurate else WHISPER_MODEL
    model = get_whisper(name)
    queued = time.monotonic()
    # One at a time per device: parallel runs fight over the same cores and all get slow.
    with _device_locks[_stt_device[name]]:
        waited = time.monotonic() - queued
        segments, _info = model.transcribe(
            audio,
            language="en" if name.endswith(".en") else None,
            beam_size=5 if accurate else 1,
            # Short commands should not be replaced by random sampling when decoding fails.
            temperature=0.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 650, "speech_pad_ms": 400},
            word_timestamps=True,
            condition_on_previous_text=False,
            hotwords=hint,
        )
        text = _speech_text(segments)
        if accurate and not text and getattr(_info, "duration_after_vad", 0) >= 0.2:
            # One deterministic retry can recover softer speech. Never fall back to
            # a rejected draft, and remove the wake hint so it cannot supply words.
            segments, _info = model.transcribe(
                audio,
                language="en" if name.endswith(".en") else None,
                beam_size=8,
                patience=1.2,
                temperature=0.0,
                vad_filter=True,
                vad_parameters={"threshold": 0.35, "min_silence_duration_ms": 650, "speech_pad_ms": 400},
                word_timestamps=True,
                condition_on_previous_text=False,
                hotwords=None,
            )
            text = _speech_text(segments)
    return text, waited, f"{name}/{_stt_device[name]}"


def louder(audio: np.ndarray) -> np.ndarray:
    """Raise a quiet microphone's level (up to 10x) so speech peaks near half scale, as Whisper expects."""
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    return audio * min(10.0, 0.5 / peak) if 0 < peak < 0.5 else audio


def loud_seconds(audio: np.ndarray) -> float:
    """How long the clip is loud: 30 ms frames within 15 dB of its loudest. A word said in a room
    is loud for well under a second; music or a TV stays loud almost all the way through."""
    frame = SAMPLE_RATE * 30 // 1000
    n = len(audio) // frame
    if not n:
        return 0.0
    energy = (audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
    return float((energy > energy.max() * 10 ** -1.5).sum() * 0.03)


def estimate_pitch(audio: np.ndarray) -> float | None:
    """Median pitch of the voiced speech in Hz, or None if there's too little of it.

    Used only to guess "Sir" or "Ma'am". Pitch is a rough clue, not a fact about a
    person: low-voiced women, high-voiced men and children will sometimes be guessed wrong.
    """
    frame, hop = 640, 160  # 40 ms frames every 10 ms at 16 kHz
    if len(audio) < frame * 6:
        return None
    count = 1 + (len(audio) - frame) // hop
    frames = audio[np.arange(frame)[None, :] + hop * np.arange(count)[:, None]]
    energy = (frames**2).mean(axis=1)
    frames = frames[energy > max(1e-6, np.percentile(energy, 80) * 0.2)]  # the louder, spoken frames
    if len(frames) < 10:
        return None
    frames = (frames - frames.mean(axis=1, keepdims=True)) * np.hanning(frame)
    acf = np.fft.irfft(np.abs(np.fft.rfft(frames, 2048)) ** 2)[:, :frame]
    acf /= acf[:, :1] + 1e-12
    lo, hi = SAMPLE_RATE // 320, SAMPLE_RATE // 60  # search 60–320 Hz, the range of adult speech
    lag = acf[:, lo:hi].argmax(axis=1) + lo
    strength = acf[np.arange(len(acf)), lag]
    # Octave check: if half the period repeats almost as strongly, the voice is an octave higher.
    rows = np.arange(len(acf))[:, None]
    near_half = np.clip(lag[:, None] // 2 + np.arange(-2, 3)[None, :], 0, frame - 1)
    half_strength = acf[rows, near_half].max(axis=1)
    half_lag = near_half[np.arange(len(acf)), acf[rows, near_half].argmax(axis=1)]
    lag = np.where((half_lag >= lo) & (half_strength > 0.92 * strength), half_lag, lag)
    voiced = strength > 0.45
    if voiced.sum() < 12:
        return None
    return float(np.median(SAMPLE_RATE / lag[voiced]))


@app.post("/api/transcribe")
async def transcribe(req: Request):
    """Body: raw 16 kHz mono 16-bit little-endian PCM from the browser.

    ?for=wake|talk|mic|request says which listener sent it. Only background wake listening
    hints her name; explicitly opening the mic or Talk means the request is for her.
    ?q=accurate uses the slower, more accurate model (for phrases meant for Nexa).
    """
    purpose = re.sub(r"[^\w-]", "", req.query_params.get("for", ""))[:16] or "?"
    accurate = req.query_params.get("q") == "accurate"
    if req.query_params.get("truncated") == "1":
        return {"text": "", "pitch": None, "retry": True, "reason": "too_long"}
    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > SAMPLE_RATE * 2 * 40:
            return JSONResponse({"error": "Recording is too long. Please speak for under 40 seconds."}, status_code=413)
    if len(raw) % 2:
        return JSONResponse({"error": "Invalid PCM recording."}, status_code=400)
    seconds = len(raw) / 2 / SAMPLE_RATE
    if len(raw) < SAMPLE_RATE // 5 * 2:  # under 0.2 s: nothing worth decoding
        return {"text": ""}
    audio = louder(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)
    matched, reason = await run_in_threadpool(owner_voice.verify, audio)
    if not matched:
        # Do not transcribe, log, or send another person's words to an AI provider.
        return {"text": "", "pitch": None, "retry": reason == "too_short", "owner": reason}
    started = time.monotonic()
    # Biasing every recording toward her name can insert it into unrelated speech.
    hint = "Nexa" if purpose == "wake" else None
    text, waited, model = await run_in_threadpool(_transcribe, audio, accurate, hint)
    # But with the hint, music or a video can come out as a lone "Nexa": one word in many seconds
    # of loud sound. A real "Nexa" is loud for under a second, even in a longer clip, so when her
    # name comes with that few words for the loud part, trust only what Whisper hears without the hint.
    loud = loud_seconds(audio)
    if hint and hint.lower() in text.lower() and len(re.findall(r"[A-Za-z']+", text)) / max(loud, 0.5) < 0.4:
        log(f"heard[{purpose}] {text!r} in {loud:.1f}s of loud sound looks made up; listening again without the hint")
        text, more, model = await run_in_threadpool(_transcribe, audio, accurate, None)
        waited += more
    took = time.monotonic() - started - waited
    pitch = estimate_pitch(audio) if text else None
    voice = f", voice ~{pitch:.0f} Hz" if pitch else ""
    log(f"heard[{purpose}] {text!r}  ({seconds:.1f}s of audio{voice}, {model}, waited {waited:.1f}s, took {took:.1f}s)")
    return {"text": text, "pitch": round(pitch) if pitch else None, "retry": not bool(text), "owner": "matched"}


@app.post("/api/log")
async def page_log(req: Request):
    """The page reports microphone and wake-word events here for troubleshooting."""
    body = await req.json()
    event = re.sub(r"[^\w-]", "", str(body.get("event", "")))[:40]
    data = json.dumps(body.get("data", {}), ensure_ascii=False)[:500]
    log(f"page {event} {data}")
    return {"ok": True}


@app.post("/api/quit")
async def quit_app(req: Request):
    """Settings → Quit Nexa: stops the app, which has no console window to close.

    Only a JSON request counts. Browsers won't send one here from another website, so a page
    you visit can't shut Nexa down.
    """
    if req.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"error": "expected JSON"}, status_code=415)
    log("quit from Settings")
    threading.Timer(0.3, os._exit, args=[0]).start()  # after this reply has gone out
    return {"ok": True}


@app.get("/api/voices")
def voices():
    return {
        "default": VOICE_NAME,
        "voices": [{"id": v, "label": VOICE_LABELS.get(v, v)} for v in installed_voices()],
        "styles": list(VOICE_STYLES),
    }


def _synthesize_kokoro(text: str, name: str, style: str) -> bytes:
    rate, pcm = 24000, np.zeros(2400, np.float32)  # "." alone: a moment of silence
    if re.search(r"\w", text):
        options = {"voice": name, "speed": 0.92 if style == "calm" else 1.0, "lang": "en-gb" if name.startswith("b") else "en-us"}
        # Longer speech needs more graphics memory, so say it in pieces of about a clause (~4 s each):
        # split at commas and colons, and between words only when a clause is still too long.
        words = []
        for clause in re.split(r"(?<=[,;:])\s+", text):
            words += clause.split(" ") if len(clause) > 70 else [clause]
        pieces, current = [], ""
        for word in words:
            if current and len(current) + len(word) > 70:
                pieces.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        pieces.append(current)
        audio = []
        with _tts_lock:
            for piece in pieces:
                try:
                    pcm, rate = get_kokoro("cuda").create(piece, **options)
                except Exception as e:  # out of graphics memory: this piece on the processor
                    log(f"casual voice on the processor for one piece (…{str(e)[-140:]})")
                    pcm, rate = get_kokoro("cpu").create(piece, **options)
                audio.append(pcm)
        pcm = np.concatenate(audio)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _synthesize(text: str, voice: str, style: str) -> bytes:
    from piper import SynthesisConfig

    if voice.startswith("kokoro:"):
        return _synthesize_kokoro(text, voice.split(":", 1)[1], style)
    piper = get_piper(voice)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        if re.search(r"\w", text):
            with _tts_lock:
                piper.synthesize_wav(text, wav, syn_config=SynthesisConfig(**VOICE_STYLES[style]))
        else:  # "." alone has nothing to say (and Piper writes nothing for it): a moment of silence
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(piper.config.sample_rate)
            wav.writeframes(bytes(piper.config.sample_rate // 10))
    return buf.getvalue()


@app.post("/api/tts")
async def tts(req: Request):
    """Body: {"text": "...", "voice": "<installed voice>", "style": "calm|natural"}. Returns WAV."""
    body = await req.json()
    text = (body.get("text") or "").strip()[:1000]
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    available = installed_voices()
    if not available:
        return JSONResponse({"error": "voice not downloaded"}, status_code=503)
    # Only names of installed voice files are accepted, never a path.
    voice = body.get("voice") if body.get("voice") in available else (
        VOICE_NAME if VOICE_NAME in available else available[0]
    )
    style = body.get("style") if body.get("style") in VOICE_STYLES else "calm"
    try:
        wav = await run_in_threadpool(_synthesize, text, voice, style)
    except Exception as e:
        log(f"tts error ({voice}): {e}")
        return JSONResponse({"error": "speech synthesis failed"}, status_code=500)
    return Response(wav, media_type="audio/wav")


# Voices a fresh install downloads: the default first, then the others in the voice menu.
PREFETCH_VOICES = [VOICE_NAME] + [
    v for v in ("en_US-hfc_female-medium", "en_US-lessac-medium", "en_GB-alan-medium", "en_US-ryan-medium")
    if v != VOICE_NAME
]


def prefetch():
    print("Preparing local owner voice recognition...")
    owner_voice.prepare()
    print(f"Downloading speech models '{WHISPER_MODEL}' and '{WHISPER_ACCURATE_MODEL}'...")
    get_whisper(WHISPER_MODEL)
    get_whisper(WHISPER_ACCURATE_MODEL)
    from piper.download_voices import download_voice

    VOICES_DIR.mkdir(exist_ok=True)
    for i, name in enumerate(PREFETCH_VOICES):
        if (VOICES_DIR / f"{name}.onnx").exists():
            continue
        print(f"Downloading voice '{name}'...")
        try:
            download_voice(name, VOICES_DIR)
        except Exception as e:
            if i == 0:
                raise  # Nexa can't speak without her default voice
            print(f"  skipped ({e})")
    # The casual voices only talk smoothly on an NVIDIA card, so they're only downloaded for one.
    if kokoro_gpu():
        KOKORO_DIR.mkdir(exist_ok=True)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            if (KOKORO_DIR / name).exists():
                continue
            print(f"Downloading casual voices ({name})...")
            try:
                with httpx.stream("GET", KOKORO_FILES + name, follow_redirects=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(KOKORO_DIR / f"{name}.part", "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                (KOKORO_DIR / f"{name}.part").replace(KOKORO_DIR / name)
            except (httpx.HTTPError, OSError) as e:
                print(f"  skipped ({e})")
    print("Speech and voice models ready.")


def open_app_window(url: str):
    """Open Nexa in her own Chrome/Edge window.

    A dedicated profile lets us pass flags that let her hear "Hey Nexa" and speak
    without a click first, and keep listening while the window is in the background.
    """
    local = Path(os.environ.get("LOCALAPPDATA", ROOT))
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        local / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    exe = next((p for p in candidates if p.exists()), None)
    if exe is None or os.environ.get("NEXA_BROWSER") == "default":
        import webbrowser

        webbrowser.open(url)
        return
    subprocess.Popen([
        str(exe),
        f"--app={url}",
        f"--user-data-dir={NEXA_HOME / 'browser'}",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        # Leave the play/pause and next keys to Spotify and other players, not her own voice.
        "--disable-features=HardwareMediaKeyHandling",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1120,900",
    ])


def nexa_running(url: str) -> bool:
    try:
        httpx.get(url, timeout=3)
        return True
    except httpx.HTTPError:
        return False


def ensure_ollama():
    """Start the local model server in the background, with no window, unless it's running."""
    try:
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return
    except httpx.HTTPError:
        pass
    exe = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "Programs/Ollama/ollama.exe"
    try:
        subprocess.Popen([str(exe) if exe.exists() else "ollama", "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        log(f"couldn't start Ollama ({e}); start it from the Start menu")


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if "--prefetch" in sys.argv:
        prefetch()
    elif "--window" in sys.argv:
        open_app_window(url)  # Nexa is already running; just open her window again
    elif "--launch" in sys.argv and nexa_running(url):
        open_app_window(url)  # the shortcut, while she's already running: just bring her window back
    else:
        if "--launch" in sys.argv:
            ensure_ollama()
        if "--open" in sys.argv or "--launch" in sys.argv:
            threading.Timer(1.5, open_app_window, args=[url]).start()
        print(f"Nexa is running at {url}  (Ctrl+C to stop)")
        print("What Nexa hears is shown below and saved to logs\\nexa.log.\n")
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
