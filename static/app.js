"use strict";

const $ = (id) => document.getElementById(id);
const els = { orb: $("orb"), orbSpot: $("orbSpot") };
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  history: [],        // [{role, content}] sent to the model each turn
  busy: false,
  abort: null,        // AbortController for the reply being generated
  speaker: null,      // SentenceSpeaker for the reply being read aloud
  web: loadPref("web", true),
  tts: false,         // Nexa's own voice (Piper) is installed on the server
  voice: loadPref("voiceNexa", ""),
  style: "natural", // Expressive delivery for the requested casual conversation style.
  echo: loadPref("echo", 1),
  sfx: loadPref("sfx", true), // anime-style sound effects
  hearing: loadPref("hearing", "accurate"), // "accurate" re-reads phrases meant for her with a bigger model
  address: loadPref("address", "none"),      // Casual by default; keep an explicitly saved title.
  addressName: loadPref("addressName", ""),
  voiceGuess: "",                            // Auto mode's latest guess: "Sir", "Ma'am" or ""
  owner: { enrolled: false, ready: false },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function loadPref(key, fallback) {
  try {
    // Settings saved before she was renamed Nexa are kept under her first name, Ciel.
    const v = localStorage.getItem("nexa." + key) ?? localStorage.getItem("ciel." + key);
    return v === null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}
function savePref(key, value) {
  try { localStorage.setItem("nexa." + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

/** Sends a troubleshooting event to the server log (logs/nexa.log). */
function report(event, data = {}) {
  fetch("/api/log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, data }),
  }).catch(() => {});
}

/* ---------- Background configuration ---------- */

let statusTimer = 0, linesWarmed = false;
async function refreshStatus() {
  let loading = false;
  try {
    const result = await (await fetch("/api/status")).json();
    state.tts = result.tts_ready;
    if (!ownerEnrollment.active) state.owner = result.owner || { enrolled: false, ready: false };
    if (state.owner.enrolled && wake.enabled) wakeLoop();
    if (state.tts && !linesWarmed) { linesWarmed = true; warmLines(); }
    loading = result.ollama && !result.model_ready;
  } catch { report("server-unavailable"); }
  clearTimeout(statusTimer);
  statusTimer = setTimeout(refreshStatus, loading ? 2000 : 10000);
}

/* ---------- How she addresses you: Sir, Ma'am, Master, a name, or nothing ---------- */

/** The title to use right now. In Auto it's the latest guess from the voice ("" until she hears you). */
function addressWord() {
  const mode = state.address;
  if (mode === "auto") return state.voiceGuess;
  if (mode === "name") return state.addressName;
  return mode === "none" ? "" : mode;
}
/** "Yes" + "?" → "Yes, Sir?" (or "Yes?" with no title). */
const titled = (start, end) => (addressWord() ? `${start}, ${addressWord()}${end}` : `${start}${end}`);

/**
 * Auto mode: guess from the pitch of the voice speaking to her. Typical men speak
 * around 85–155 Hz and women around 165–255 Hz; in between she uses no title.
 */
function noteVoice(pitch) {
  if (state.address !== "auto" || !pitch) return;
  // Judge by the middle of the last three readings so one odd reading (a video playing
  // in the background, a laugh) can't flip Sir to Ma'am.
  state.pitches = [...(state.pitches || []).slice(-2), pitch];
  const sorted = [...state.pitches].sort((a, b) => a - b);
  const typical = sorted[Math.floor(sorted.length / 2)];
  const guess = typical < 150 ? "Sir" : typical > 162 ? "Ma'am" : "";
  if (guess !== state.voiceGuess) {
    state.voiceGuess = guess;
    report("voice-guess", { pitch, guess: guess || "none" });
  }
}

/* ---------- Browser audio needs one click or key press before it may start ---------- */

let gestureWait = null;
function userGesture() {
  if (!gestureWait) {
    gestureWait = new Promise((resolve) => {
      const go = () => {
        window.removeEventListener("pointerdown", go, true);
        window.removeEventListener("keydown", go, true);
        gestureWait = null;
        resolve();
      };
      window.addEventListener("pointerdown", go, true);
      window.addEventListener("keydown", go, true);
    });
  }
  return gestureWait;
}
async function whenRunning(ctx) {
  if (ctx.state === "running") return;
  ctx.resume().catch(() => {});
  await sleep(250);
  while (ctx.state !== "running") {
    // Nexa's own window allows sound right away; a normal browser tab needs a click.
    report("waiting-for-click", { ctx: ctx.state });
    await userGesture();
    await ctx.resume().catch(() => {});
  }
}

/* ---------- Nexa's voice: offline neural TTS through an "inner voice" effect ---------- */

const ECHO_LEVELS = [[0, 0], [0.28, 0.2], [0.42, 0.4]]; // [shimmer, echo] for Off/Subtle/Strong

/** Keep the orb and microphone echo guard in sync with actual playback. */
const speaking = {
  count: 0, quietUntil: 0,
  start() { this.count++; orb.setMode("speaking"); },
  end() {
    this.count = Math.max(0, this.count - 1);
    this.quietUntil = performance.now() + 600;
    if (!this.count) orb.setMode(state.busy ? "thinking" : wake.enabled ? "listening" : "idle");
  },
};

const audio = {
  ctx: null, input: null, analyser: null, data: null, source: null,

  init() {
    if (this.ctx) return this.ctx;
    const ctx = this.ctx = new AudioContext();
    this.input = ctx.createGain();
    const mix = ctx.createGain();
    // A faint shimmer (slowly modulated short delay) and a soft echo make the
    // voice sound like it is speaking inside your head.
    const shimmer = ctx.createDelay(0.05);
    shimmer.delayTime.value = 0.011;
    const lfo = ctx.createOscillator();
    const depth = ctx.createGain();
    lfo.frequency.value = 0.4;
    depth.gain.value = 0.0018;
    lfo.connect(depth).connect(shimmer.delayTime);
    lfo.start();
    this.shimmerLevel = ctx.createGain();
    const echo = ctx.createConvolver();
    echo.buffer = impulse(ctx, 1.8, 3.2);
    this.echoLevel = ctx.createGain();
    this.setEcho(state.echo);
    const air = ctx.createBiquadFilter();
    air.type = "highshelf";
    air.frequency.value = 5000;
    air.gain.value = 3;
    this.input.connect(mix);
    this.input.connect(shimmer).connect(this.shimmerLevel).connect(mix);
    this.input.connect(echo).connect(this.echoLevel).connect(mix);
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.data = new Float32Array(this.analyser.fftSize);
    mix.connect(air).connect(this.analyser).connect(ctx.destination);
    return ctx;
  },

  setEcho(level) {
    if (!this.ctx) return;
    const [shimmer, echo] = ECHO_LEVELS[level] || ECHO_LEVELS[1];
    this.shimmerLevel.gain.value = shimmer;
    this.echoLevel.gain.value = echo;
  },

  /** The last ~20 ms of Nexa's voice (drives the orb's waveform). */
  wave() {
    if (!this.analyser) return null;
    this.analyser.getFloatTimeDomainData(this.data);
    return this.data;
  },

  /** Loudness of what Nexa is saying right now, 0..1. */
  level() {
    const d = this.wave();
    if (!d) return 0;
    let sum = 0;
    for (let i = 0; i < d.length; i++) sum += d[i] * d[i];
    return Math.min(1, Math.sqrt(sum / d.length) * 3);
  },

  async play(buffer) {
    const ctx = this.init();
    await whenRunning(ctx);
    return new Promise((resolve) => {
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(this.input);
      src.onended = () => {
        if (this.source === src) this.source = null;
        speaking.end();
        resolve();
      };
      this.source = src;
      speaking.start();
      try { src.start(); }
      catch (e) {
        src.onended = null;
        this.source = null;
        speaking.end();
        throw e;
      }
    });
  },

  stop() {
    try { this.source?.stop(); } catch { /* already stopped */ }
    this.source = null;
  },
};

function impulse(ctx, seconds, decay) {
  const len = Math.floor(ctx.sampleRate * seconds);
  const buf = ctx.createBuffer(2, len, ctx.sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
  }
  return buf;
}

/* ---------- Sound effects (made here with Web Audio, so they work offline) ---------- */

const sfx = {
  bus: null, white: null, analyzing: 0,

  /** The effects' own output, with a long reverb so they ring like her inner world. Null when off. */
  out() {
    const ctx = audio.init();
    if (!state.sfx || ctx.state !== "running") return null;
    if (!this.bus) {
      this.bus = ctx.createGain();
      this.bus.gain.value = 0.9;
      const verb = ctx.createConvolver();
      verb.buffer = impulse(ctx, 2.6, 2.4);
      const wet = ctx.createGain();
      wet.gain.value = 0.5;
      this.bus.connect(ctx.destination);
      this.bus.connect(verb).connect(wet).connect(ctx.destination);
    }
    return this.bus;
  },

  tone(freq, at, dur, vol, { type = "sine", to = 0 } = {}) {
    const bus = this.out();
    if (!bus) return false;
    const ctx = audio.ctx, t = ctx.currentTime + at;
    const osc = ctx.createOscillator(), env = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    if (to) osc.frequency.exponentialRampToValueAtTime(to, t + dur);
    env.gain.setValueAtTime(0, t);
    env.gain.linearRampToValueAtTime(vol, t + Math.min(0.01, dur / 4));
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(env).connect(bus);
    osc.start(t);
    osc.stop(t + dur + 0.05);
    return true;
  },

  /** A glassy bell (FM synthesis). */
  bell(freq, at, { dur = 1.2, vol = 0.12, ratio = 3.5, index = 2.2 } = {}) {
    const bus = this.out();
    if (!bus) return false;
    const ctx = audio.ctx, t = ctx.currentTime + at;
    const car = ctx.createOscillator(), mod = ctx.createOscillator(), depth = ctx.createGain(), env = ctx.createGain();
    car.frequency.value = freq;
    mod.frequency.value = freq * ratio;
    depth.gain.setValueAtTime(freq * index, t);
    depth.gain.exponentialRampToValueAtTime(freq * 0.05, t + dur * 0.6);
    env.gain.setValueAtTime(0, t);
    env.gain.linearRampToValueAtTime(vol, t + 0.006);
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    mod.connect(depth).connect(car.frequency);
    car.connect(env).connect(bus);
    for (const o of [car, mod]) { o.start(t); o.stop(t + dur + 0.05); }
    return true;
  },

  /** Filtered noise sweeping from one pitch to another: a whoosh. */
  whoosh(at, dur, vol, from, to, q = 1.2) {
    const bus = this.out();
    if (!bus) return false;
    const ctx = audio.ctx, t = ctx.currentTime + at;
    if (!this.white) {
      this.white = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
      const d = this.white.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    const src = ctx.createBufferSource(), band = ctx.createBiquadFilter(), env = ctx.createGain();
    src.buffer = this.white;
    band.type = "bandpass";
    band.Q.value = q;
    band.frequency.setValueAtTime(from, t);
    band.frequency.exponentialRampToValueAtTime(to, t + dur);
    env.gain.setValueAtTime(0, t);
    env.gain.linearRampToValueAtTime(vol, t + dur * 0.4);
    env.gain.linearRampToValueAtTime(0, t + dur);
    src.connect(band).connect(env).connect(bus);
    src.start(t);
    src.stop(t + dur + 0.05);
    return true;
  },

  /** "I heard you": a quick two-note digital chime. */
  heard() {
    this.tone(1567.98, 0, 0.12, 0.09);
    this.tone(2093, 0.085, 0.25, 0.09);
  },

  /** Just before Nexa speaks: a crystalline "message" chime. True if it played. */
  notice() {
    const played = [880, 1318.5, 1760, 2637].map((f, i) => this.bell(f, i * 0.055, { dur: 1.4 - i * 0.15, vol: 0.07 }));
    this.whoosh(0, 0.5, 0.025, 3000, 9000, 3);
    return played.some(Boolean);
  },

  /** The magic circle opening: a low bloom, a rising whoosh and a shimmer of bells. */
  summon() {
    this.tone(55, 0, 0.9, 0.22, { to: 110 });
    this.whoosh(0, 0.7, 0.06, 300, 5000);
    [1046.5, 1568, 2093, 3136].forEach((f, i) => this.bell(f, 0.25 + i * 0.07, { dur: 1.6, vol: 0.05, ratio: 2.01, index: 1.2 }));
  },

  /** The circle closing. */
  dismiss() {
    this.whoosh(0, 0.6, 0.05, 5000, 300);
    this.bell(1568, 0.05, { dur: 1.0, vol: 0.06 });
    this.bell(1046.5, 0.16, { dur: 1.2, vol: 0.06 });
  },

  /** Searching the web: a sonar-like ping. */
  search() {
    this.tone(1244.5, 0, 0.5, 0.07, { to: 1100 });
    this.tone(1864.7, 0.14, 0.6, 0.04, { to: 1700 });
  },

  /** Thinking: soft data ticks until it's turned off. */
  analyze(on) {
    if (!on) { clearInterval(this.analyzing); this.analyzing = 0; return; }
    if (this.analyzing || !this.out()) return;
    const notes = [2093, 2349, 2637, 3136, 3520, 4186];
    this.analyzing = setInterval(() => {
      if (Math.random() < 0.65) this.tone(notes[(Math.random() * notes.length) | 0], 0, 0.04, 0.025, { type: "triangle" });
    }, 90);
  },
};

const clipCache = new Map(); // short, often-repeated lines like "Yes, Sir?"
function synthesize(text) {
  const key = `${state.voice}|${state.style}|${text}`;
  if (clipCache.has(key)) return clipCache.get(key);
  const clip = fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice: state.voice, style: state.style }),
  })
    .then((r) => { if (!r.ok) throw new Error("tts failed"); return r.arrayBuffer(); })
    .then((bytes) => audio.init().decodeAudioData(bytes));
  if (text.length <= 40) {
    clipCache.set(key, clip);
    clip.catch(() => clipCache.delete(key));
  }
  return clip;
}
function warmLines() {
  // Prepare her short replies so they play the instant she hears "Hey Nexa".
  if (state.tts) {
    // Every title she might use right after hearing her name.
    const titles = state.address === "auto" ? ["Sir", "Ma'am", ""] : [addressWord()];
    const lines = titles.flatMap((t) => [t ? `Yes, ${t}?` : "Yes?", t ? `Talk to you later, ${t}.` : "Talk to you later."]);
    [...lines, "Got it."].forEach((line) => synthesize(line).catch(() => {}));
  }
}

// Fallback when Nexa's voice isn't installed: Windows' built-in offline voices.
let fallbackVoice = null;
function pickFallbackVoice() {
  const local = speechSynthesis.getVoices().filter((v) => v.localService);
  fallbackVoice = local.find((v) => /zira|aria|jenny|hazel|susan/i.test(v.name))
    || local.find((v) => v.lang.startsWith("en")) || local[0] || null;
}
if ("speechSynthesis" in window) {
  pickFallbackVoice();
  speechSynthesis.addEventListener("voiceschanged", pickFallbackVoice);
}

function cleanForSpeech(s) {
  return s
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\s*\[\d{1,2}(?:\s*,\s*\d{1,2})*\]/g, "")
    .replace(/[*_#`>~|]/g, "")
    .replace(/^\s*[-+]\s+/gm, "")
    .replace(/\p{Extended_Pictographic}/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Speaks streamed text one sentence at a time so speech starts before the reply
 * ends. onstart(sentence) fires as each sentence begins (used for subtitles).
 */
class SentenceSpeaker {
  /** chime: ring a soft chime before the first sentence. */
  constructor({ chime = true } = {}) {
    this.chime = chime;
    this.buf = "";
    this.pending = 0;
    this.ended = false;
    this.cancelled = false;
    this.inCode = false;
    this.queue = Promise.resolve();
    this.utterances = []; // Chrome drops onend events for utterances it garbage-collects
    this.finished = new Promise((resolve) => { this.resolve = resolve; });
  }
  push(text) {
    this.buf += text;
    let m;
    while ((m = this.buf.match(/^[\s\S]*?(?:[.!?](?=\s)|\n)/))) {
      this.buf = this.buf.slice(m[0].length);
      this.say(m[0]);
    }
  }
  say(segment) {
    if (/```/.test(segment)) { this.inCode = !this.inCode; return; }
    if (this.inCode || this.cancelled) return;
    const text = cleanForSpeech(segment);
    if (!text) return;
    this.pending++;
    const done = () => { this.pending--; this.check(); };
    const clip = state.tts
      ? synthesize(text).catch((e) => { report("tts-error", { message: String(e) }); return null; })
      : Promise.resolve(null);
    this.queue = this.queue.then(async () => {
        const buffer = await clip;
        if (this.cancelled) return;
        if (this.chime) {
          this.chime = false;
          if (sfx.notice()) await sleep(320);
          if (this.cancelled) return;
        }
        if (buffer) {
          try {
            this.onstart?.(text);
            await audio.play(buffer);
            return;
          } catch (e) { report("playback-error", { message: String(e) }); }
        }
        await this.speakFallback(text);
      }).catch((e) => report("speech-error", { message: String(e) })).finally(done);
  }
  speakFallback(text) {
    if (this.cancelled || !("speechSynthesis" in window)) return Promise.resolve();
    return new Promise((resolve) => {
      const u = new SpeechSynthesisUtterance(text);
      let started = false, finished = false;
      if (fallbackVoice) { u.voice = fallbackVoice; u.lang = fallbackVoice.lang; }
      u.rate = 0.95;
      u.onstart = () => {
        if (finished || this.cancelled) return;
        started = true;
        speaking.start();
        this.onstart?.(text);
      };
      u.onend = u.onerror = () => {
        if (finished) return;
        finished = true;
        if (started) speaking.end();
        resolve();
      };
      this.utterances.push(u);
      speechSynthesis.speak(u);
    });
  }
  end() {
    if (this.buf) this.say(this.buf);
    this.buf = "";
    this.ended = true;
    this.check();
  }
  check() {
    if (this.ended && this.pending <= 0) this.resolve();
  }
  cancel() {
    this.cancelled = true;
    this.ended = true;
    this.pending = 0;
    audio.stop();
    // Some browsers drop cancellation events. Release our speaking state explicitly.
    for (const utterance of this.utterances) utterance.onend?.();
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    speaking.quietUntil = performance.now() + 600;
    this.resolve();
  }
}

function stopSpeaking() {
  state.speaker?.cancel();
  state.speaker = null;
}

/** Says a fixed line in Nexa's voice and waits until it has been spoken. */
async function sayLine(text, onstart, { chime = true } = {}) {
  stopSpeaking();
  const sp = state.speaker = new SentenceSpeaker({ chime });
  sp.onstart = onstart;
  sp.push(text);
  sp.end();
  await sp.finished;
  if (state.speaker === sp) state.speaker = null;
}

/* ---------- Microphone: one shared stream, many short listeners ---------- */

const mic = {
  stream: null, ctx: null, proc: null, rate: 48000, noise: 0.003, last: null,
  listeners: new Set(), closeTimer: 0, stat: { max: 0, sum: 0, n: 0 },

  async open() {
    clearTimeout(this.closeTimer);
    if (!this.ctx) {
      // Listeners that start together share one microphone instead of opening two.
      this.opening ||= this.connect().finally(() => { this.opening = null; });
      await this.opening;
    }
    await whenRunning(this.ctx);
  },
  async connect() {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      report("mic-error", { name: e.name, message: e.message });
      throw e;
    }
    this.ctx = new AudioContext();
    this.rate = this.ctx.sampleRate;
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.proc = this.ctx.createScriptProcessor(4096, 1, 1);
    this.proc.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      this.last = data.slice(); // for the orb's waveform
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length);
      this.stat.max = Math.max(this.stat.max, rms);
      this.stat.sum += rms;
      this.stat.n++;
      for (const fn of [...this.listeners]) fn(data, rms);
    };
    source.connect(this.proc);
    this.proc.connect(this.ctx.destination);
    const track = this.stream.getAudioTracks()[0];
    report("mic-open", { device: track?.label, rate: this.rate, ctx: this.ctx.state });
  },
  add(fn) {
    clearTimeout(this.closeTimer);
    this.listeners.add(fn);
  },
  remove(fn) {
    this.listeners.delete(fn);
    // Release the mic (and the browser's recording light) when nobody is listening.
    if (!this.listeners.size) this.closeTimer = setTimeout(() => this.close(), 2500);
  },
  close() {
    if (this.listeners.size || !this.ctx) return;
    this.proc.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    this.ctx.close();
    this.ctx = this.stream = this.proc = this.last = null;
  },
};

// Every 20 s while the mic is open, log how loud the room is (helps tune the wake word).
setInterval(() => {
  if (!mic.ctx || !mic.stat.n) return;
  report("mic-level", {
    max: +mic.stat.max.toFixed(4), avg: +(mic.stat.sum / mic.stat.n).toFixed(4),
    noise: +mic.noise.toFixed(4), ctx: mic.ctx.state, wake: wake.enabled,
  });
  mic.stat = { max: 0, sum: 0, n: 0 };
}, 20000);

/**
 * Captures one spoken phrase from the shared mic. `result` resolves to 16 kHz
 * Int16 PCM once the speaker pauses, or to null if nothing was said in maxIdleMs.
 */
class Listener {
  constructor({ onLevel, maxIdleMs = Infinity, endSilenceMs = 1800, minVoicedMs = 300, maxSpeechMs = 35000 } = {}) {
    Object.assign(this, { onLevel, maxIdleMs, endSilenceMs, minVoicedMs, maxSpeechMs });
    this.chunks = [];
    this.done = false;
    this.result = new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; });
    this.begin();
  }

  async begin() {
    try {
      await mic.open();
    } catch (e) {
      this.done = true;
      this.reject(e);
      return;
    }
    if (this.done) { mic.remove(null); return; } // cancelled while the mic was opening
    const rate = mic.rate;
    const preRoll = Math.ceil(600 / (4096 / rate * 1000)); // keep ~0.6 s before speech starts
    // Measure recorded samples: delayed browser callbacks must not manufacture a pause.
    let elapsed = 0, speechAt = null, lastVoice = 0, voiced = 0;

    this.handler = (data, rms) => {
      // A recording that starts during a reply must never contain Nexa's own voice.
      if (speaking.count || performance.now() < speaking.quietUntil) {
        this.chunks = [];
        speechAt = null;
        elapsed = lastVoice = voiced = 0;
        this.onLevel?.(0);
        return;
      }
      this.chunks.push(new Float32Array(data));
      this.onLevel?.(Math.min(1, rms * 14));
      const chunkMs = data.length / rate * 1000;
      const now = elapsed += chunkMs;
      const startLevel = Math.max(0.004, mic.noise * 2.5);

      if (speechAt === null) {
        // Learn the room's background level: follow it down quickly, up slowly.
        // Speech onset must not raise the background threshold for quieter words.
        if (rms <= startLevel) mic.noise = rms < mic.noise ? mic.noise * 0.7 + rms * 0.3 : mic.noise * 0.97 + rms * 0.03;
        if (this.chunks.length > preRoll) this.chunks.splice(0, this.chunks.length - preRoll);
        if (rms > startLevel) { speechAt = lastVoice = now; voiced = chunkMs; }
        else if (now > this.maxIdleMs) this.cancel();
        return;
      }
      // A loud first word must not hide a softly spoken end of the sentence.
      if (rms > Math.max(0.002, mic.noise * 1.5)) { lastVoice = now; voiced += chunkMs; }
      if (now - lastVoice > this.endSilenceMs) {
        if (voiced < this.minVoicedMs) { speechAt = null; voiced = 0; return; } // a click or cough
        this.stop();
      } else if (now - speechAt > this.maxSpeechMs) {
        this.stop({ truncated: true });
      }
    };
    mic.add(this.handler);
  }

  /** Finish now and keep what was heard. */
  stop({ truncated = false } = {}) {
    const pcm = toPcm16(this.chunks, mic.rate);
    pcm.truncated = truncated;
    this.finish(pcm);
  }
  /** Finish now and discard everything. */
  cancel() { this.finish(null); }

  finish(value) {
    if (this.done) return;
    this.done = true;
    if (this.handler) mic.remove(this.handler);
    this.onLevel?.(0);
    this.resolve(value);
  }
}

/**
 * The phrase as 16 kHz, 16-bit PCM for Whisper. A windowed-sinc low-pass filter removes what
 * 16 kHz can't hold before resampling; without it, hiss above 8 kHz folds back over the speech.
 */
function toPcm16(chunks, rate) {
  const len = chunks.reduce((n, c) => n + c.length, 0);
  const x = new Float32Array(len);
  let offset = 0;
  for (const c of chunks) { x.set(c, offset); offset += c.length; }
  const ratio = rate / 16000;
  const out = new Int16Array(Math.floor(len / ratio));
  const clip = (v) => Math.max(-1, Math.min(1, v)) * 0x7fff;
  if (ratio <= 1) { for (let i = 0; i < out.length; i++) out[i] = clip(x[Math.floor(i * ratio)]); return out; }
  // Filter taps for 64 fractional positions between input samples, computed once.
  const fc = 0.45 / ratio, half = Math.ceil(10 * ratio), taps = 2 * half, steps = 64;
  const table = new Float32Array((steps + 1) * taps);
  for (let p = 0; p <= steps; p++) {
    for (let k = 0; k < taps; k++) {
      const d = p / steps + half - 1 - k; // distance from this tap to the output sample
      if (Math.abs(d) >= half) continue;
      const sinc = d === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * d) / (Math.PI * d);
      table[p * taps + k] = sinc * (0.5 + 0.5 * Math.cos((Math.PI * d) / half));
    }
  }
  for (let i = 0; i < out.length; i++) {
    const t = i * ratio, c = Math.floor(t), row = Math.round((t - c) * steps) * taps, first = c - half + 1;
    let s = 0;
    for (let k = Math.max(0, -first); k < taps && first + k < len; k++) s += x[first + k] * table[row + k];
    out[i] = clip(s);
  }
  return out;
}

/**
 * quality "fast" checks phrases for her name; "accurate" re-reads phrases meant for her.
 * purpose ("wake", "request", "mic"…) only labels the server's log line.
 */
async function transcribe(pcm, purpose = "", quality = "fast") {
  if (!pcm || !pcm.length) return { text: "", pitch: null };
  if (pcm.truncated) return { text: "", pitch: null, retry: true, reason: "too_long" };
  const r = await fetch(`/api/transcribe?for=${purpose}&q=${quality}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: pcm, // send just this view, including when it is a slice of a recording
  });
  if (!r.ok) throw new Error("transcribe failed");
  const res = await r.json();
  return { text: res.text || "", pitch: res.pitch || null, retry: !!res.retry, owner: res.owner || "", reason: res.reason || "" };
}

/* ---------- Drawing helpers for the orb ---------- */

// Nexa's colors: mint, emerald, and aqua while she listens.
// Shared by WebGL and the 2D fallback. Find nearby links once with a spatial hash,
// rather than comparing every particle with every other particle during animation.
function createOrbCloud(count) {
  let seed = 20261104;
  const rand = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const points = [], edges = [], cells = new Map(), cellSize = 0.18;
  const key = (x, y, z) => `${x},${y},${z}`;
  for (let i = 0; i < count; i++) {
    const y = rand() * 2 - 1, angle = rand() * Math.PI * 2;
    const radial = Math.sqrt(1 - y * y);
    const r = i % 11 === 0 ? 1 + rand() * 0.12 : Math.pow(rand(), i % 5 === 0 ? 0.6 : 1 / 3);
    const x = Math.cos(angle) * radial * r, py = y * r, z = Math.sin(angle) * radial * r;
    const point = { x, y: py, z, r, seed: rand(), size: rand(), band: Math.floor((angle / (Math.PI * 2)) * 63) };
    points.push(point);
    const gx = Math.floor(x / cellSize), gy = Math.floor(py / cellSize), gz = Math.floor(z / cellSize);
    const candidates = [];
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
      for (const j of cells.get(key(gx + dx, gy + dy, gz + dz)) || []) {
        const q = points[j], dist = Math.hypot(x - q.x, py - q.y, z - q.z);
        if (dist < cellSize && dist > 0.035) candidates.push({ j, dist });
      }
    }
    candidates.sort((a, b) => a.dist - b.dist);
    for (const near of candidates.slice(0, i % 3 === 0 ? 3 : 2)) {
      edges.push({ a: i, b: near.j, alpha: (1 - near.dist / cellSize) * 0.15 + 0.025 });
    }
    const cellKey = key(gx, gy, gz);
    if (!cells.has(cellKey)) cells.set(cellKey, []);
    cells.get(cellKey).push(i);
  }
  return { points, edges };
}

const AQUA = [100, 235, 215];
const rgba = (c, a) => `rgba(${c[0] | 0}, ${c[1] | 0}, ${c[2] | 0}, ${Math.max(0, Math.min(1, a))})`;
const mixColor = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
const easeOutBack = (x) => 1 + 2.70158 * (x - 1) ** 3 + 1.70158 * (x - 1) ** 2;

/* ---------- Particle-cloud fallback for PCs without WebGL ---------- */

class Orb {
  /** compact: the small orb on the welcome screen. anchor: the element it sits on. */
  constructor(canvas, { compact = false, anchor = null, world = false } = {}) {
    this.canvas = canvas;
    this.g = canvas.getContext("2d");
    this.compact = compact;
    this.world = world;
    this.anchor = anchor || canvas;
    this.mode = "idle";
    this.level = 0;
    this.input = 0;
    this.t = 0;
    this.listenAmt = 0;
    this.thinkAmt = 0;
    this.speakAmt = 0;
    this.intro = 1;
    this.pulses = [];
    this.waveSource = null; // () => Float32Array of recent audio samples, or null
    this.running = false;
    this.cloud = createOrbCloud(compact ? 350 : 800);
    this.projected = new Float32Array(this.cloud.points.length * 4);
    this.frame = this.frame.bind(this);
    new ResizeObserver(() => this.resize()).observe(canvas);
    if (this.anchor !== canvas) new ResizeObserver(() => this.resize()).observe(this.anchor);
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width) return;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(rect.width * this.dpr);
    this.canvas.height = Math.round(rect.height * this.dpr);
    this.place();
  }

  place() {
    const rect = this.canvas.getBoundingClientRect();
    const spot = this.anchor.getBoundingClientRect();
    this.cx = (spot.left + spot.width / 2 - rect.left) * this.dpr;
    this.cy = (spot.top + spot.height / 2 - rect.top) * this.dpr;
    this.size = Math.min(spot.width, spot.height) * this.dpr;
  }

  setMode(mode) { this.mode = mode; }
  setLevel(v) { this.input = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0; }
  /** Briefly brighten the volume on a voice-state change. */
  pulse(strength = 1) { if (!REDUCED) this.pulses.push({ r: 0.5, a: 0.3 * strength }); }

  start({ intro = false } = {}) {
    if (intro) { this.intro = REDUCED ? 1 : 0; this.pulse(0.5); }
    if (this.running) return;
    this.running = true;
    this.resize();
    this.last = performance.now();
    this.raf = requestAnimationFrame(this.frame);
  }
  stop() { this.running = false; cancelAnimationFrame(this.raf); }

  frame(now) {
    if (!this.running) return;
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    if (document.hidden) { this.raf = requestAnimationFrame(this.frame); return; }
    this.place(); // Follow the compositor transform as the orb docks beneath a display.
    if (!REDUCED) this.t += dt;
    const m = this.mode;
    const target = m === "speaking" ? audio.level() : m === "listening" || this.compact ? this.input : 0;
    this.level += (target - this.level) * (1 - Math.exp(-dt * (target > this.level ? 14 : 4)));
    const ease = 1 - Math.exp(-dt * 4);
    this.listenAmt += (+(m === "listening") - this.listenAmt) * ease;
    this.thinkAmt += (+(m === "thinking") - this.thinkAmt) * ease;
    this.speakAmt += (+(m === "speaking") - this.speakAmt) * ease;
    this.intro = Math.min(1, this.intro + dt / 0.9);
    for (const p of this.pulses) { p.r += dt * 0.8; p.a -= dt * 0.6; }
    this.pulses = this.pulses.filter((p) => p.a > 0);
    if (this.size) this.draw();
    this.raf = requestAnimationFrame(this.frame);
  }

  draw() {
    const { g, t, dpr, cx, cy, cloud, projected } = this;
    const W = this.canvas.width, H = this.canvas.height;
    const shown = 1 - Math.pow(1 - this.intro, 3);
    const R = this.size * 0.48 * 0.94;
    const L = this.level, listen = this.listenAmt, speak = this.speakAmt;
    const ice = mixColor([116, 212, 185], AQUA, listen * 0.5);
    g.clearRect(0, 0, W, H);
    if (this.world) {
      g.fillStyle = '#111c1a';
      g.fillRect(0, 0, W, H);
    }
    const pulseLight = this.pulses.reduce((sum, pulse) => sum + pulse.a, 0);
    const haze = g.createRadialGradient(cx, cy, 0, cx, cy, R * 1.6);
    haze.addColorStop(0, rgba(ice, (0.12 + L * speak * 0.07 + pulseLight * 0.08) * shown));
    haze.addColorStop(0.55, rgba(ice, 0.035 * shown));
    haze.addColorStop(1, rgba(ice, 0));
    g.fillStyle = haze;
    g.fillRect(0, 0, W, H);

    // A reusable sprite avoids creating hundreds of gradients every frame.
    if (!this.sprite) {
      this.sprite = document.createElement('canvas');
      this.sprite.width = this.sprite.height = 32;
      const sg = this.sprite.getContext('2d');
      const glow = sg.createRadialGradient(16, 16, 0, 16, 16, 16);
      glow.addColorStop(0, 'rgba(210,255,237,1)');
      glow.addColorStop(0.18, 'rgba(159,240,213,.85)');
      glow.addColorStop(0.4, 'rgba(101,206,177,.3)');
      glow.addColorStop(1, 'rgba(101,206,177,0)');
      sg.fillStyle = glow;
      sg.fillRect(0, 0, 32, 32);
    }
    const turn = t * (0.055 + this.thinkAmt * 0.085), c = Math.cos(turn), sn = Math.sin(turn);
    const motion = REDUCED ? 0 : 1;
    const breath = 1 + (Math.sin(t * 0.7) * 0.008 + L * (listen * 0.035 + speak * 0.065)) * motion;
    const wave = this.waveSource?.();
    for (let i = 0; i < cloud.points.length; i++) {
      const p = cloud.points[i], at = i * 4;
      const sample = wave?.length ? wave[Math.floor(p.band / 64 * wave.length)] || 0 : 0;
      const scale = breath + Math.max(-1, Math.min(1, sample * 6)) * L * 0.05 * motion;
      const x = (p.x * c + p.z * sn) * scale, z = (p.z * c - p.x * sn) * scale;
      const perspective = 4.8 / (5 - z), depth = Math.max(0, Math.min(1, (z + 1.1) / 2.2));
      const reveal = Math.max(0, Math.min(1, (this.intro * 1.45 - p.r * 0.4) / 0.55));
      projected[at] = cx + x * perspective * R;
      projected[at + 1] = cy - p.y * scale * perspective * R;
      projected[at + 2] = (0.18 + depth * 0.65) * reveal * (0.7 + Math.sin(t * 0.8 + p.seed * 70) * 0.18);
      projected[at + 3] = perspective;
    }
    g.save();
    g.globalCompositeOperation = 'lighter';
    g.strokeStyle = rgba(ice, 1);
    g.lineWidth = Math.max(0.5, dpr * 0.55);
    for (const edge of cloud.edges) {
      const a = edge.a * 4, b = edge.b * 4;
      g.globalAlpha = edge.alpha * Math.min(projected[a + 2], projected[b + 2]) * shown;
      g.beginPath();
      g.moveTo(projected[a], projected[a + 1]);
      g.lineTo(projected[b], projected[b + 1]);
      g.stroke();
    }
    const dotScale = Math.max(0.48, Math.min(1.35, R / (240 * dpr))) * dpr;
    for (let i = 0; i < cloud.points.length; i++) {
      const p = cloud.points[i], at = i * 4;
      const size = (3.5 + p.size * p.size * 7) * dotScale * projected[at + 3];
      g.globalAlpha = projected[at + 2] * shown;
      g.drawImage(this.sprite, projected[at] - size / 2, projected[at + 1] - size / 2, size, size);
    }
    g.restore();
  }
}

// Use one full-screen orb in every state; WebGL has a flat-canvas fallback.
const ORB_3D = typeof Orb3D !== "undefined" && Orb3D.probe();
report("orb", { kind: ORB_3D ? "3d" : "flat" });
const options = { anchor: els.orbSpot, world: true };
const orb = ORB_3D ? new Orb3D(els.orb, options) : new Orb(els.orb, options);
orb.waveSource = () => orb.mode === "speaking" ? audio.wave() : orb.mode === "listening" ? mic.last : null;

const hud = new NexaHUD.Controller({
  panel: $("hud"), title: $("hudTitle"), body: $("hudBody"),
  status: $("hudStatus"), sources: $("hudSources"), close: $("hudClose"),
}, open => {
  els.orbSpot.classList.toggle("docked", open);
  if (!open && document.activeElement === $("hudClose")) els.orb.focus({ preventScroll: true });
});

/* ---------- Voice replies ---------- */

function setVoiceState(text) { report("voice-state", { text }); }
function setBusy(busy) { state.busy = busy; }

/** Streams a spoken reply; conversation context stays in memory for follow-ups. */
async function ask(text, { display = false, onSpeaking, onStatus, onError } = {}) {
  text = text.trim();
  if (!text || state.busy) return "";
  const displayGeneration = display ? hud.begin(text) : 0;
  stopSpeaking();
  state.history.push({ role: "user", content: text });
  const ctrl = state.abort = new AbortController();
  setBusy(true);
  setVoiceState("Thinking…");
  orb.setMode("thinking");
  const speaker = state.speaker = new SentenceSpeaker();
  speaker.onstart = (sentence) => {
    setVoiceState("Speaking…");
    orb.setMode("speaking");
    onSpeaking?.(sentence);
  };
  let reply = "", error = "", screenReply = "", sources = [];
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "auto", messages: state.history, voice: true, web: state.web,
        address: addressWord(), display,
      }),
      signal: ctrl.signal,
    });
    if (!r.ok || !r.body) throw new Error("bad response");
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (ctrl.signal.aborted) break;
      buf += done ? decoder.decode() + "\n" : decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.error) { error = ev.error; continue; }
        if (ev.status) {
          setVoiceState(ev.status);
          if (display) hud.status(ev.status, displayGeneration);
          onStatus?.(ev.status);
        }
        if (Array.isArray(ev.sources)) sources = ev.sources;
        if (display && ev.display && typeof ev.display.content === "string") {
          screenReply = ev.display.content;
          hud.show(screenReply, sources, displayGeneration);
        }
        if (typeof ev.t === "string") {
          reply += ev.t;
          speaker.push(ev.t);
        }
      }
      if (done) break;
    }
  } catch (e) {
    if (!ctrl.signal.aborted) error = "Couldn't reach Nexa's server. Check that it's running and try again.";
  }

  if (reply || screenReply) state.history.push({ role: "assistant", content: screenReply || reply, sources });
  else state.history.pop();
  if (error && !ctrl.signal.aborted) {
    if (display) hud.error(error, displayGeneration);
    setVoiceState(error);
    onError?.(error);
    speaker.push(error);
  }
  if (display && ctrl.signal.aborted && !screenReply) hud.error("That reply was interrupted. Ask me to show it again.", displayGeneration);
  else if (display && !screenReply && !error) {
    if (reply) hud.show(reply, sources, displayGeneration);
    else hud.error("I couldn't get that content. Try asking again.", displayGeneration);
  }
  try {
    speaker.end();
    await speaker.finished;
  } finally {
    if (state.speaker === speaker) state.speaker = null;
    state.abort = null;
    setBusy(false);
    orb.setMode(wake.enabled ? "listening" : "idle");
    setVoiceState(error || "Ready when you are.");
  }
  return reply;
}

function stopGeneration() {
  state.abort?.abort();
  stopSpeaking();
}

/* ---------- "Hey Nexa" wake word ---------- */

// Background listening needs her name, and it only counts when it's said *to* her:
// after a greeting ("Hey Nexa"), at the start ("Nexa, ..." / "Nexa open Word"), or at the
// end ("..., Nexa?" / "turn up the volume Nexa"). "I read about Nexa today" never wakes her.
const GREETING_RE = /^(?:he+y+|hi+|ha+y|hei|hel+o|ok(?:ay)?|oi|yo|a|ay|eh)$/i;
const END_RE = /^(?:ok(?:ay)?[\s,]*)?(?:stop|goodbye|good bye|bye(?: bye)?|that'?s all|that is all|never ?mind|cancel|dismiss(?:ed)?|go to sleep|thank you,? that'?s all)[\s.!]*$/i;
// Words that start a request right after her name ("Nexa what time is it", "Nexa open Word").
const LEAD_RE = /^(?:what'?s?|who'?s?|how|when|where|why|which|can|could|would|will|should|please|tell|open|play|search|set|turn|show|display|hide|clear|dismiss|visuali[sz]e|put|bring|give|find|read|remind|call|help|i|are|do|does|did|stop|start|make|write|explain|translate|check|close|launch|run|go|pause|resume|skip|next|mute|unmute|lock|volume|increase|decrease|raise|lower|switch|shut|restart|look|google|say|define|calculate|convert)$/i;
// Words that make her name part of a sentence about her rather than a call ("about Nexa", "buy a Nexa").
const MENTION_RE = /^(?:about|to|with|from|of|for|by|at|on|in|into|the|a|an|this|that|my|your|our|their|his|her|called|named|like|than|and|or|is|was|meet|use|using)$/i;
// Polite words that may follow her name at the end ("open Spotify Nexa please").
const POLITE_RE = /^(?:please|thanks|thank|you|now)$/i;
// Her name, and the spellings Whisper may write for it with different accents ("Nexxa", "Neksa").
const isName = (word) => /^(?:nexa|nexxa|neksa|nexah|nexar|nexia)(?:'?s)?$/i.test(word);
// "Nex-a" / "Neks a" -> "Nexa" (but not "Next, a", which is a real phrase).
const joinName = (text) => text.replace(/\bne(?:x|ks)[\s-]+a(h?)\b/gi, "Nexa$1");
const isGreetingOnly = (text) => { const w = text.match(/[A-Za-z']+/g); return w?.length === 1 && GREETING_RE.test(w[0]); };

/**
 * Nexa answers when she is spoken to by name. Returns what to answer, or null:
 *   "Hey Nexa, what time is it?" / "Nexa, what time is it?" -> { rest: "what time is it?" }
 *   "Hey Nexa" / "Nexa?"                                    -> { rest: "" } (she says "Yes, Sir?")
 *   "What time is it, Nexa?" / "open Spotify Nexa please"     -> { rest: "What time is it?" } / { rest: "open Spotify please" }
 */
function matchWake(text) {
  text = joinName(text);
  const words = [...text.matchAll(/[A-Za-z']+/g)];
  // Only her name, maybe said more than once ("Nexa… Nexa!"): she answers "Yes, Sir?" and
  // listens for the request.
  if (words.some((w) => isName(w[0])) && words.every((w) => isName(w[0]) || GREETING_RE.test(w[0]))) return { rest: "" };
  const after = (w) => text.slice(w.index + w[0].length).replace(/^[\s,.!?;:-]+/, "").trim();
  // After a greeting. "A", "ay" and "eh" (how Whisper often writes "Hey") count only at the start,
  // so "I'd buy a Nexa" isn't a call.
  for (let i = 0; i + 1 < words.length; i++) {
    if (GREETING_RE.test(words[i][0]) && isName(words[i + 1][0]) && (i === 0 || !/^(?:a|ay|eh)$/i.test(words[i][0]))) {
      return { rest: after(words[i + 1]) };
    }
  }
  if (words.length && isName(words[0][0])) {
    const tail = text.slice(words[0].index + words[0][0].length);
    if (!tail.trim() || /^[,.!?-]/.test(tail) || (words[1] && LEAD_RE.test(words[1][0]))) return { rest: after(words[0]) };
  }
  // At the end, maybe followed by "please" or "thanks". Without a comma before it, the word before
  // must not make it a mention ("talking about Nexa").
  let k = words.length - 1;
  while (k > 0 && POLITE_RE.test(words[k][0])) k--;
  if (k > 0 && isName(words[k][0])) {
    const before = text.slice(0, words[k].index);
    if (/,\s*$/.test(before) || !MENTION_RE.test(words[k - 1][0])) {
      const request = before.replace(/[\s,]+$/, "");
      const tail = text.slice(words[k].index + words[k][0].length).replace(/^[\s,]+/, "").trim();
      return { rest: /^[.!?]*$/.test(tail) ? request + (tail || ".") : `${request} ${tail}` };
    }
  }
  return null;
}

const wake = { enabled: false, listener: null, running: false, triggering: false, checking: 0, greeting: null, generation: 0 };

/** Two recordings as one, with a short silence between. */
function joinPcm(a, b) {
  const gap = 3200; // 0.2 s at 16 kHz
  const out = new Int16Array(a.length + gap + b.length);
  out.set(a);
  out.set(b, a.length + gap);
  out.truncated = !!(a.truncated || b.truncated) || out.length > 16000 * 40;
  return out;
}

function setWakeLevel(v) {
  orb.setLevel(v);
}

function setWake(on) {
  wake.generation++;
  wake.enabled = on;
  savePref("wake", on);
  orb.setMode(on ? "listening" : "idle");
  report("wake", { on });
  if (on) wakeLoop();
  else wake.listener?.cancel();
}

/** Listens in the background, phrase by phrase; answers only phrases with her name. */
async function wakeLoop() {
  if (wake.running) return;
  wake.running = true;
  try {
    while (wake.enabled && state.owner.enrolled && !ownerEnrollment.active && !session.on) {
      // Allow a natural pause after her name and enough time for the complete request.
      const listener = wake.listener = new Listener({
        onLevel: setWakeLevel, endSilenceMs: 1800, minVoicedMs: 350, maxSpeechMs: 35000,
      });
      let pcm;
      try {
        pcm = await listener.result;
      } catch (e) {
        setWake(false);
        report("microphone-unavailable", { message: e.name === "NotAllowedError"
          ? "Microphone blocked. Allow the microphone in the browser, then tap the orb."
          : "No microphone found. Plug one in or check Windows sound settings, then try again." });
        return;
      }
      wake.listener = null;
      // Skip phrases heard while Nexa is talking or a conversation is active.
      if (!pcm || !wake.enabled || session.on || state.speaker) continue;
      // In constant chatter (TV, a call), don't build a backlog of phrases to check.
      if (wake.checking >= 2) { report("wake-skip", { reason: "busy" }); continue; }
      // "Hey" … pause … "Nexa": hear both together, so her name isn't a lone word without context.
      if (wake.greeting && performance.now() - wake.greeting.at < 5000) pcm = joinPcm(wake.greeting.pcm, pcm);
      wake.greeting = null;
      checkWakePhrase(pcm); // transcribe in the background and keep listening meanwhile
    }
  } finally {
    wake.running = false;
    wake.listener = null;
  }
}

async function checkWakePhrase(pcm) {
  const generation = wake.generation;
  let text = "", pitch = null, owner = "";
  wake.checking++;
  // With Hearing set to Accurate, the bigger model also listens for her name: the fast one
  // often mishears real voices ("Hey Nexa" → "Hey, see you", "A C").
  const quality = state.hearing === "accurate" ? "accurate" : "fast";
  try { ({ text, pitch, owner } = await transcribe(pcm, "wake", quality)); } catch { return; } finally { wake.checking--; }
  if (owner !== "matched") {
    if (owner) report("wake-rejected", { reason: owner });
    return;
  }
  if (!text || generation !== wake.generation) return;
  const hit = matchWake(text);
  if (hit) noteVoice(pitch); // the person who called her decides Sir or Ma'am
  report("wake-check", { text, matched: !!hit });
  if (!hit) {
    if (isGreetingOnly(text)) wake.greeting = { pcm, at: performance.now() }; // her name may come next

    return;
  }
  if (!wake.enabled || ownerEnrollment.active || session.on || state.speaker || state.busy || wake.triggering) return;
  wake.triggering = true;
  wake.listener?.cancel();
  sfx.heard();
  // Already heard with the accurate model? Then there's nothing to re-read.
  const reread = quality === "fast" ? pcm : null;
  try { await startSession("wake", { command: hit.rest, pcm: reread }); } finally { wake.triggering = false; }
}

/* ---------- Voice session: talk with Nexa ---------- */

const session = { on: false, id: 0, mode: "", listener: null };

function setOrb(mode) {
  orb.setMode(mode);
  sfx.analyze(mode === "thinking");
}
function reportSpeech(you, nexa) {
  report("speech", { you, nexa });
}

/**
 * mode "wake": she heard her name. She answers that one request, then goes back
 * to waiting, so the next request needs her name again. If only her name was
 * said, she replies "Yes, Sir?" (or Ma'am…) and takes the next sentence as the request.
 * mode "live" (tap the orb): a conversation until goodbye or Escape.
 *
 * pcm: the recording that contained her name, re-read with the accurate model.
 */
async function startSession(mode, { command = "", pcm = null } = {}) {
  if (ownerEnrollment.active || !requireOwner()) return;
  stopGeneration();
  wake.listener?.cancel();
  while (state.busy) await sleep(30); // let a stopped reply finish cleaning up
  const id = ++session.id;
  const alive = () => session.on && session.id === id;
  Object.assign(session, { on: true, mode, listener: null });
  orb.pulse(0.5);
  sfx.summon();
  setOrb("idle", "Linked");
  reportSpeech("", "");

  if (mode === "wake") {
    if (command && pcm && state.hearing === "accurate") {
      reportSpeech(`“${command}”`, "");
      setOrb("thinking", "Listening closely");
      command = await reread(pcm);
      if (!alive()) return;
    }
    report("session", { mode, command });
    if (!command) {
      await acknowledge(alive);
      if (alive()) command = await listenForRequest(alive, 8000);
    }
    if (alive() && command) await handleRequest(command, alive);
    if (alive()) endSession();
    return;
  }

  report("session", { mode });
  reportSpeech("", "Speak naturally. You don't need to say my name. Say “goodbye” to finish.");
  while (alive()) {
    const request = await listenForRequest(alive, Infinity);
    if (!alive()) return;
    if (request === null) { endSession(); return; }
    if (request) await handleRequest(request, alive);
  }
}

/** A rejected accurate reading must never turn back into an actionable fast draft. */
async function reread(pcm) {
  try {
    const { text: better } = await transcribe(pcm, "request", "accurate");
    if (!better) return "";
    const hit = matchWake(better);
    return hit ? hit.rest : better;
  } catch {
    return "";
  }
}

/**
 * Listens for one request. Returns its text, "" if nothing was said (or, with needName, if it
 * wasn't said with her name), or null if the mic failed.
 */
async function listenForRequest(alive, maxIdleMs, needName = false) {
  while (alive()) {
    setOrb("listening", "Listening");
    const listener = session.listener = new Listener({ onLevel: (v) => orb.setLevel(v), maxIdleMs });
    let pcm;
    try {
      pcm = await listener.result;
    } catch {
      setOrb("idle", "Microphone unavailable");
      reportSpeech("", "Allow microphone access, then try again.");
      return null;
    } finally {
      session.listener = null;
    }
    if (!pcm || !alive()) return "";
    setOrb("thinking", "Analyzing");
    let text = "", reason = "";
    try {
      const heard = await transcribe(pcm, needName ? "talk" : "request", state.hearing);
      if (!alive()) return "";
      if (heard.owner && heard.owner !== "matched") {
        reportSpeech("", ownerMessage(heard.owner));
        if (["not_enrolled", "unavailable"].includes(heard.owner)) return null;
        return ""; // stay silent for a different speaker
      }
      text = heard.text;
      reason = heard.reason;
      noteVoice(heard.pitch);
    } catch {
      if (alive()) {
        reportSpeech("", "I couldn't transcribe that. Check the server, then try again.");
        await sleep(1000);
      }
      return "";
    }
    if (!alive()) return "";
    if (!text) {
      const message = reason === "too_long"
        ? "That was too long. Please try a shorter request."
        : "I didn't catch that clearly. Could you say it again?";
      reportSpeech("", message);
      await sayLine(message, undefined, { chime: false });
      continue;
    }
    const hit = matchWake(text);
    // Only her name: "Yes, Sir?", and the next sentence is for her even without it.
    if (hit && !hit.rest) { await acknowledge(alive); needName = false; continue; }
    if (hit || !needName) return hit ? hit.rest : text;
    report("talk-skip", { text });
    reportSpeech(`“${text}”`, "Say “Nexa” when it's for me.");
    return "";
  }
  return "";
}

async function handleRequest(command, alive) {
  const presentation = NexaHUD.intent(command);
  if (presentation === "hide" && hud.open) {
    hud.hide();
    await sayLine("Okay, cleared.", undefined, { chime: false });
    return;
  }
  if (presentation === "recall") {
    const last = [...state.history].reverse().find(message => message.role === "assistant");
    if (last) {
      hud.begin(command);
      hud.show(last.content, last.sources || []);
      await sayLine("Here you go.", undefined, { chime: false });
    } else await sayLine("What would you like me to show?", undefined, { chime: false });
    return;
  }
  if (presentation === "show" && NexaHUD.isClockRequest(command)) {
    hud.begin(command);
    hud.clock();
    await sayLine("Yep, here's the time.", undefined, { chime: false });
    return;
  }
  if (END_RE.test(command)) {
    reportSpeech(`“${command}”`, "");
    setOrb("speaking", "Speaking");
    await sayLine("Alright, catch you later.", (s) => reportSpeech(undefined, s), { chime: false });
    if (alive()) endSession();
    return;
  }
  await converse(command, alive, { display: presentation === "show" });
}

/** "Yes, Sir?" — her answer when you only say her name. */
async function acknowledge(alive) {
  setOrb("speaking", "Speaking");
  const line = titled("Yeah", "?");
  reportSpeech(undefined, line);
  if (state.tts || "speechSynthesis" in window) await sayLine(line, undefined, { chime: false });
  else sfx.heard();
  if (alive()) reportSpeech(undefined, "");
}

async function converse(text, alive, { display = false } = {}) {
  reportSpeech(`“${text}”`, "");
  setOrb("thinking", "Analyzing");
  let spoke = false;
  const reply = await ask(text, {
    display,
    onError: (error) => { if (alive()) reportSpeech(undefined, error); },
    onStatus: (s) => {
      if (!alive()) return;
      if (s.startsWith("Searching")) sfx.search();
      setOrb("thinking", s.startsWith("Searching") ? "Searching the web" : "Analyzing");
    },
    onSpeaking: (sentence) => {
      if (!alive()) return;
      if (!spoke) { spoke = true; orb.pulse(0.6); }
      setOrb("speaking", "Speaking");
      reportSpeech(undefined, sentence); // subtitles follow her voice, sentence by sentence
    },
  });
  // If she couldn't speak (voice unavailable), still show the answer as a subtitle.
  if (alive() && !spoke && reply) reportSpeech(undefined, cleanForSpeech(reply));
}

function interruptSession() {
  if (session.listener) return; // already listening
  stopGeneration();              // her answer stops; the session moves on
}

/** End the conversation while the same orb keeps animating. */
function endSession() {
  session.on = false;
  session.id++;
  session.listener?.cancel();
  session.listener = null;
  stopGeneration();
  sfx.analyze(false);
  setOrb(wake.enabled ? "listening" : "idle");
  if (wake.enabled) wakeLoop();
}

/* ---------- Saved voice preferences ---------- */
async function loadVoices() {
  try {
    const result = await (await fetch("/api/voices")).json();
    const ids = result.voices.map(voice => voice.id);
    if (!ids.includes(state.voice)) state.voice = ids.includes(result.default) ? result.default : ids[0] || "";
  } catch { /* the server uses its configured voice */ }
}

/* Owner enrollment pauses all other listeners; samples live only in memory. */
const OWNER_PROMPT = "Hey Nexa, this is my voice, and I'm ready to talk with you.";
const ownerEnrollment = { active: false, busy: false, saving: false, samples: [], listener: null, id: 0 };

function ownerMessage(reason) {
  return {
    not_enrolled: "Tap the orb to set up your voice before speaking to Nexa.",
    not_owner: "That voice didn't match your profile. Try again in a quiet room.",
    too_short: "Please say a complete sentence so I can check your voice.",
    unavailable: "Voice checking is unavailable. Run setup.ps1, then restart Nexa.",
  }[reason] || "I didn't catch that clearly. Please try again.";
}

function requireOwner() {
  if (state.owner.enrolled) return true;
  if (!ownerEnrollment.active) enrollOwner();
  return false;
}

function cancelEnrollment() {
  if (ownerEnrollment.saving) return;
  ownerEnrollment.id++;
  ownerEnrollment.active = ownerEnrollment.busy = false;
  ownerEnrollment.listener?.cancel();
  ownerEnrollment.listener = null;
  ownerEnrollment.samples = [];
  stopSpeaking();
  setOrb(wake.enabled && state.owner.enrolled ? "listening" : "idle");
  if (wake.enabled) wakeLoop();
}

function pcmBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

/** First-run enrollment is spoken, so setup needs no visible interface. */
async function enrollOwner() {
  if (ownerEnrollment.active || state.owner.enrolled) return;
  ownerEnrollment.active = ownerEnrollment.busy = true;
  const id = ++ownerEnrollment.id;
  const active = () => ownerEnrollment.active && ownerEnrollment.id === id;
  wake.generation++;
  wake.listener?.cancel();
  endSession();
  try {
    setOrb("thinking");
    const prepared = await fetch("/api/owner/prepare", { method: "POST" });
    const setup = await prepared.json();
    if (!active()) return;
    if (!prepared.ok) throw new Error(setup.error || "Voice recognition could not start. Please restart Nexa.");
    setOrb("speaking");
    await sayLine("Repeat this once, then pause. " + OWNER_PROMPT);
    if (!active()) return;
    setOrb("listening");
    const listener = ownerEnrollment.listener = new Listener({
      onLevel: v => orb.setLevel(v), maxIdleMs: 15000, maxSpeechMs: 18000, endSilenceMs: 1600,
    });
    const pcm = await listener.result;
    if (!active()) return;
    ownerEnrollment.listener = null;
    if (!pcm || pcm.truncated || pcm.length < 16000 * 2) throw new Error("I didn't catch the whole sentence. Tap the orb to try again.");
    ownerEnrollment.samples.push(pcmBase64(pcm));
    setOrb("thinking");
    ownerEnrollment.saving = true;
    const response = await fetch("/api/owner/enroll", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ samples: ownerEnrollment.samples }),
    });
    const result = await response.json();
    ownerEnrollment.saving = false;
    if (!active()) return;
    if (!response.ok) throw new Error(result.error || "Voice setup failed. Tap the orb to try again.");
    state.owner = result;
    await sayLine("You're ready. Say Hey Nexa whenever you need me.");
  } catch (error) {
    ownerEnrollment.saving = false;
    if (active()) {
      report("enrollment-error", { message: error.message });
      await sayLine(error.name === "NotAllowedError"
        ? "Allow microphone access in your browser, then tap the orb to try again."
        : error.message || "Voice setup failed. Tap the orb to try again.");
    }
  } finally {
    if (active()) cancelEnrollment();
  }
}

async function activateOrb() {
  await initialized;
  if (ownerEnrollment.active) return;
  if (session.on) { interruptSession(); return; }
  if (!state.owner.enrolled) { await enrollOwner(); return; }
  if (!wake.enabled) setWake(true);
  await startSession("live");
}

els.orb.addEventListener("click", activateOrb);
els.orb.addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateOrb(); }
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (ownerEnrollment.active) cancelEnrollment();
  else { if (hud.open) hud.hide(); endSession(); }
});

report("page-load", {
  wake: loadPref("wake", true),
  standalone: matchMedia("(display-mode: standalone)").matches,
  ua: navigator.userAgent.replace(/^Mozilla\/5\.0 /, "").slice(0, 90),
});
navigator.permissions?.query({ name: "microphone" }).then((p) => {
  report("mic-permission", { state: p.state });
  p.onchange = () => report("mic-permission", { state: p.state });
}).catch(() => {});

orb.start({ intro: true });
const initialized = Promise.all([loadVoices(), refreshStatus()]);
setWake(true); // voice activation stays available without a visible switch
