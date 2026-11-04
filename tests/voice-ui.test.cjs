const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `Missing source section: ${start} ... ${end}`);
  return source.slice(a, b);
};

function speakerContext(tts = true) {
  const spoken = [], played = [];
  const synthesis = {
    speak(u) { spoken.push(u.text); u.onstart?.(); setImmediate(() => u.onend?.()); },
    cancel() {},
  };
  const context = vm.createContext({
    state: { tts }, fallbackVoice: null,
    window: { speechSynthesis: synthesis }, speechSynthesis: synthesis,
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    speaking: { count: 0, start() { this.count++; }, end() { this.count--; } },
    synthesize: () => Promise.reject(new Error('TTS unavailable')),
    report() {}, cleanForSpeech: s => s.trim(), sleep: async () => {}, sfx: { notice: () => false },
    audio: { play: async buffer => played.push(buffer), stop() {} }, performance,
  });
  vm.runInContext(section('class SentenceSpeaker {', 'function stopSpeaking()') + '\nthis.Speaker = SentenceSpeaker;', context);
  return { context, spoken, played };
}

test('failed server speech falls back sentence by sentence in order', async () => {
  const { context, spoken } = speakerContext();
  const speaker = new context.Speaker({ chime: false });
  speaker.push('Hello owner. How are you?'); speaker.end();
  await speaker.finished;
  assert.deepEqual(spoken, ['Hello owner.', 'How are you?']);
  assert.equal(context.speaking.count, 0);
});

test('failed audio playback also uses the browser voice', async () => {
  const { context, spoken } = speakerContext();
  context.synthesize = async () => ({});
  context.audio.play = async () => { throw new Error('playback failed'); };
  const speaker = new context.Speaker({ chime: false });
  speaker.push('Hello.'); speaker.end();
  await speaker.finished;
  assert.deepEqual(spoken, ['Hello.']);
});

test('cancelling a pending voice prevents late playback', async () => {
  const { context, spoken, played } = speakerContext();
  let deliver;
  context.synthesize = () => new Promise(resolve => { deliver = resolve; });
  const speaker = new context.Speaker();
  speaker.push('Hello.'); speaker.end(); speaker.cancel(); deliver({});
  await speaker.queue;
  assert.deepEqual(spoken, []); assert.deepEqual(played, []);
});

test('cancelling browser speech releases speaking state even without a browser cancellation event', async () => {
  const { context } = speakerContext(false);
  context.speechSynthesis.speak = u => { u.onstart(); };
  const speaker = new context.Speaker({ chime: false });
  speaker.push('Hello.'); speaker.end();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.speaking.count, 1);
  speaker.cancel();
  await speaker.queue;
  assert.equal(context.speaking.count, 0);
});

function replyContext(events) {
  const { context, spoken } = speakerContext(false);
  const requests = [], modes = [], statuses = [], displays = [];
  Object.assign(context.state, { history: [], busy: false, web: true, prefer: 'groq' });
  Object.assign(context, {
    els: { model: { value: 'auto' }, mic: {}, liveBtn: {}, stopVoice: {}, voiceState: {} },
    orb: { setMode: mode => modes.push(mode) }, wake: { enabled: true },
    hud: { begin: () => 1, show: content => displays.push(content), status() {}, error() {} },
    report: (event, data) => { if (event === "voice-state") statuses.push(data.text); },
    addressWord: () => 'Sir', AbortController, TextDecoder,
    stopSpeaking() { context.state.speaker?.cancel(); context.state.speaker = null; },
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      // A streamed event can span arbitrary byte boundaries, including inside UTF-8.
      const data = Buffer.from(events.map(e => JSON.stringify(e)).join('\n'));
      let index = 0;
      return { ok: true, body: { getReader: () => ({ read: async () => index < data.length
        ? { value: data.subarray(index, ++index), done: false } : { done: true } }) } };
    },
  });
  vm.runInContext(section('function setVoiceState(', '/* ---------- "Hey Nexa"') +
    '\nthis.ask = ask; this.stop = stopGeneration;', context);
  return { context, spoken, requests, modes, statuses, displays };
}

test('background requests accept HUD verbs immediately after Nexa', () => {
  const context = vm.createContext({});
  vm.runInContext(section('const GREETING_RE =', 'const wake =') + '\nthis.match = matchWake;', context);
  for (const command of ['hide the display', 'display the forecast', 'clear the screen', 'dismiss this panel', 'visualize the result', 'put that on screen']) {
    assert.equal(context.match('Nexa ' + command).rest, command);
  }
});

test('voice replies stream directly to speech and retain context for follow-ups', async () => {
  const { context, spoken, requests, modes } = replyContext([
    { status: 'Searching the web' }, { sources: [{ title: 'Example' }] }, { brain: 'Groq' },
    { t: 'Hello, Sir. ' }, { t: 'Enjoy your café.' },
  ]);
  const statuses = [];
  assert.equal(await context.ask(' Hello ', { onStatus: text => statuses.push(text) }),
    'Hello, Sir. Enjoy your café.');
  assert.deepEqual(spoken, ['Hello, Sir.', 'Enjoy your café.']);
  assert.deepEqual(statuses, ['Searching the web']);
  assert.equal(requests[0].body.voice, true);
  assert.deepEqual(requests[0].body.messages, [{ role: 'user', content: 'Hello' }]);
  assert.equal(context.state.history[1].role, 'assistant');
  assert.equal(context.state.busy, false);
  assert.equal(modes.at(-1), 'listening');
  await context.ask('And tomorrow?');
  assert.equal(requests[1].body.messages.length, 3);
});

test('provider errors are spoken without retaining an unanswered turn', async () => {
  const { context, spoken, statuses } = replyContext([{ error: 'The model is unavailable. Try again.' }]);
  const errors = [];
  assert.equal(await context.ask('Hello', { onError: text => errors.push(text) }), '');
  assert.equal(errors.length, 1);
  assert.equal(spoken.join(' '), errors[0]);
  assert.equal(statuses.at(-1), errors[0]);
  assert.equal(context.state.history.length, 0);
  assert.equal(context.state.busy, false);
});

test('screen payloads cannot open the display during ordinary conversation', async () => {
  const { context, displays, requests } = replyContext([{ display: { content: '# Private panel' } }, { t: 'Hello.' }]);
  await context.ask('How are you?');
  assert.equal(requests[0].body.display, false);
  assert.deepEqual(displays, []);
});

test('explicit display keeps rich content on screen and only speaks the casual summary', async () => {
  const content = '# Comparison\n| A | B |\n|---|---|\n| 1 | 2 |';
  const { context, displays, spoken, requests } = replyContext([
    { display: { content } }, { t: 'Yep, here is the comparison.' },
  ]);
  await context.ask('Show me a comparison', { display: true });
  assert.equal(requests[0].body.display, true);
  assert.deepEqual(displays, [content]);
  assert.deepEqual(spoken, ['Yep, here is the comparison.']);
  assert.equal(context.state.history[1].content, content);
});

test('interrupting a spoken reply releases the next voice turn and cancels playback', async () => {
  const { context } = replyContext([{ t: 'Hello. How are you?' }]);
  context.speechSynthesis.speak = u => { u.onstart(); };
  const pending = context.ask('Hello');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.state.busy, true);
  context.stop();
  await pending;
  assert.equal(context.state.busy, false);
  assert.equal(context.speaking.count, 0);
});

test('an unavailable server gives a spoken recovery instruction', async () => {
  const { context, spoken } = replyContext([]);
  context.fetch = async () => { throw new Error('connection refused'); };
  await context.ask('Hello');
  assert.match(spoken.join(' '), /Check that it's running and try again/);
  assert.equal(context.state.busy, false);
  assert.equal(context.state.history.length, 0);
});

test('the complete app boots with the HUD hidden and the orb centered', async () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const events = new Map();
  const elements = Object.fromEntries(ids.map(id => [id, {
    getContext: () => ({}),
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: 600, width: 600, height: 600 }),
    addEventListener: (type, callback) => events.set(`${id}:${type}`, callback),
    classList: { toggle() {} }, hidden: id === 'hud', dataset: {},
  }]));
  const context = vm.createContext({
    document: { getElementById: id => { assert.ok(elements[id], `Missing element ${id}`); return elements[id]; }, addEventListener() {} },
    window: { devicePixelRatio: 1 }, navigator: { userAgent: 'test' },
    localStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false }), ResizeObserver: class { observe() {} },
    performance, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    fetch: async url => ({ json: async () => url === '/api/status'
      ? { tts_ready: false, owner: { enrolled: false }, ollama: false }
      : { voices: [], default: '' } }),
  });
  vm.runInContext(fs.readFileSync('static/hud.js', 'utf8'), context);
  vm.runInContext(source, context);
  await vm.runInContext('initialized', context);
  assert.equal(elements.hud.hidden, true);
  assert.equal(vm.runInContext('hud.open', context), false);
  assert.ok(events.has('orb:click'));
  assert.equal(vm.runInContext('orb.running', context), true);
  assert.equal(vm.runInContext('wake.enabled', context), true);
  assert.equal(/<textarea|id="settings"|id="composer"/.test(html), false);
});

function enrollmentContext(record) {
  const requests = [], spoken = [];
  const context = vm.createContext({
    state: { owner: { enrolled: false, ready: false } },
    wake: { enabled: true, generation: 0 }, orb: { setLevel() {} },
    endSession() {}, stopSpeaking() {}, wakeLoop() {}, setOrb() {}, report() {},
    sayLine: async text => spoken.push(text),
    Listener: class {
      constructor() { this.result = record ? record(context) : Promise.resolve(new Int16Array(64000)); }
      cancel() {}
    },
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ enrolled: url.endsWith('/enroll'), ready: true }) };
    },
  });
  vm.runInContext(section('const OWNER_PROMPT =', 'async function activateOrb(') +
    '\nthis.enroll = enrollOwner; this.cancel = cancelEnrollment; this.enrollment = ownerEnrollment;', context);
  return { context, requests, spoken };
}

test('spoken setup records one sentence and discards it after saving', async () => {
  const { context, requests, spoken } = enrollmentContext();
  await context.enroll();
  assert.equal(requests.length, 2);
  assert.equal(JSON.parse(requests[1].options.body).samples.length, 1);
  assert.equal(context.state.owner.enrolled, true);
  assert.equal(context.enrollment.samples.length, 0);
  assert.equal(context.enrollment.active, false);
  assert.equal(spoken.filter(text => text.startsWith('Repeat this once, then pause.')).length, 1);
  assert.equal(spoken.length, 2); // one prompt, one confirmation
});

test('enrollment does not record or prompt again for an existing profile', async () => {
  const { context, requests, spoken } = enrollmentContext();
  context.state.owner.enrolled = true;
  await context.enroll();
  assert.equal(requests.length, 0);
  assert.equal(spoken.length, 0);
});

test('failed or incomplete setup never saves a profile and can be retried', async () => {
  const incomplete = new Int16Array(64000);
  incomplete.truncated = true;
  for (const pcm of [null, new Int16Array(16000), incomplete]) {
    const { context, requests, spoken } = enrollmentContext(() => Promise.resolve(pcm));
    await context.enroll();
    assert.equal(requests.length, 1);
    assert.equal(context.state.owner.enrolled, false);
    assert.equal(context.enrollment.active, false);
    assert.equal(context.enrollment.samples.length, 0);
    assert.match(spoken.at(-1), /Tap the orb to try again/);
  }
});

test('cancelling spoken enrollment cannot save a late recording', async () => {
  let deliver;
  const { context, requests } = enrollmentContext(() => new Promise(resolve => { deliver = resolve; }));
  const pending = context.enroll();
  await new Promise(resolve => setImmediate(resolve));
  context.cancel();
  deliver(new Int16Array(64000));
  await pending;
  assert.equal(requests.length, 1);
  assert.equal(context.state.owner.enrolled, false);
  assert.equal(context.enrollment.samples.length, 0);
});

function wakeContext(heard) {
  const sessions = [];
  const context = vm.createContext({
    state: { hearing: 'accurate' }, session: { on: false }, ownerEnrollment: { active: false },
    performance, noteVoice() {}, report() {}, sfx: { heard() {} },
    transcribe: async () => heard,
    startSession: async (mode, options) => sessions.push({ mode, ...options }),
  });
  vm.runInContext(section('const GREETING_RE =', 'const wake =') +
    section('const wake =', '/** Two recordings as one,') +
    section('async function checkWakePhrase(', '/* ---------- Voice session:') +
    '\nwake.enabled = true; this.check = checkWakePhrase; this.wake = wake;', context);
  return { context, sessions };
}

test('a verified name call wakes a session that listens for a separate request', async () => {
  const { context, sessions } = wakeContext({ text: 'Nexa', owner: 'matched' });
  await context.check(new Int16Array(32000));
  assert.equal(sessions.length, 1);
  assert.equal(sessions[0].mode, 'wake');
  assert.equal(sessions[0].command, '');
  assert.equal(context.wake.checking, 0);
});

test('an unverified voice cannot wake a session even if a name appears in the response', async () => {
  for (const owner of ['not_owner', 'not_enrolled', 'too_short', 'unavailable']) {
    const { context, sessions } = wakeContext({ text: 'Nexa, open YouTube', owner });
    await context.check(new Int16Array(32000));
    assert.equal(sessions.length, 0);
  }
});

async function listenerContext(options = {}) {
  let wallTime = 0;
  const mic = {
    rate: 16000, noise: 0.001, handler: null,
    open: async () => {}, add(fn) { this.handler = fn; },
    remove(fn) { if (this.handler === fn) this.handler = null; },
  };
  const context = vm.createContext({
    mic, speaking: { count: 0, quietUntil: 0 },
    performance: { now: () => wallTime },
    toPcm16: chunks => new Int16Array(chunks.reduce((n, c) => n + c.length, 0)),
  });
  vm.runInContext(section('class Listener {', '/**\n * The phrase as 16 kHz') + '\nthis.Listener = Listener;', context);
  const listener = new context.Listener(options);
  await new Promise(resolve => setImmediate(resolve));
  return {
    listener, context, mic,
    feed(rms, count = 1, wallDelay = 100) {
      for (let i = 0; i < count; i++) {
        wallTime += wallDelay;
        mic.handler?.(new Float32Array(1600).fill(rms), rms);
      }
    },
  };
}

test('a natural pause stays in the same recorded turn', async () => {
  const { listener, feed } = await listenerContext();
  feed(0.03, 6); feed(0.0001, 14);
  assert.equal(listener.done, false);
  feed(0.03, 6); feed(0.0001, 19);
  const pcm = await listener.result;
  assert.equal(pcm.truncated, false);
  assert.equal(pcm.length, 45 * 1600);
});

test('quiet words after a loud onset are still captured', async () => {
  const { listener, feed } = await listenerContext();
  feed(0.4, 4); feed(0.008, 25);
  assert.equal(listener.done, false);
  feed(0.0001, 19);
  assert.equal((await listener.result).length, 48 * 1600);
});

test('delayed background callbacks do not cut off speech or manufacture idle time', async () => {
  const { listener, feed } = await listenerContext({ maxIdleMs: 8000 });
  feed(0.0001, 1, 15000);
  assert.equal(listener.done, false);
  feed(0.03, 5); feed(0.0001, 1, 6000);
  assert.equal(listener.done, false);
  feed(0.03, 4); feed(0.0001, 19);
  assert.equal((await listener.result).truncated, false);
});

test('a time-limited recording is marked incomplete and never submitted for recognition', async () => {
  const { listener, feed, context } = await listenerContext({ maxSpeechMs: 500 });
  feed(0.03, 7);
  const pcm = await listener.result;
  assert.equal(pcm.truncated, true);
  context.fetch = () => { throw new Error('Incomplete recording was submitted'); };
  vm.runInContext(section('async function transcribe(pcm', '/* ---------- Drawing helpers') + '\nthis.decode = transcribe;', context);
  const result = await context.decode(pcm, 'request', 'accurate');
  assert.equal(result.text, '');
  assert.equal(result.reason, 'too_long');
});

test('joining a greeting cannot lose an incomplete recording marker', () => {
  const context = vm.createContext({});
  vm.runInContext(section('function joinPcm(', 'function setWakeLevel(') + '\nthis.join = joinPcm;', context);
  const greeting = new Int16Array(16000), request = new Int16Array(32000);
  request.truncated = true;
  assert.equal(context.join(greeting, request).truncated, true);
  assert.equal(context.join(greeting, new Int16Array(32000)).truncated, false);
  assert.equal(context.join(greeting, new Int16Array(40 * 16000)).truncated, true);
});

test('Nexa speech and its echo are excluded from the next recording', async () => {
  const { listener, feed, context } = await listenerContext();
  feed(0.03, 4);
  context.speaking.count = 1;
  feed(0.2, 8);
  context.speaking.count = 0;
  feed(0.03, 5); feed(0.0001, 19);
  assert.equal((await listener.result).length, 24 * 1600);
});

test('an uncertain accurate reread cannot fall back to an actionable fast draft', async () => {
  const context = vm.createContext({ transcribe: async () => ({ text: '' }), matchWake: () => null });
  vm.runInContext(section('async function reread(', '/**\n * Listens for one request.') + '\nthis.reread = reread;', context);
  assert.equal(await context.reread(new Int16Array(16000), 'delete my files'), '');
  context.transcribe = async () => { throw new Error('decoder unavailable'); };
  assert.equal(await context.reread(new Int16Array(16000), 'delete my files'), '');
});

test('a repeat request keeps a wake session listening until a complete turn is heard', async () => {
  const requests = [{ text: '', owner: 'matched' }, { text: 'What time is it?', owner: 'matched' }];
  const spoken = [];
  let recordings = 0;
  const context = vm.createContext({
    session: { listener: null }, state: { hearing: 'accurate' }, orb: { setLevel() {} },
    Listener: class { constructor() { recordings++; this.result = Promise.resolve(new Int16Array(16000)); } },
    setOrb() {}, reportSpeech() {}, noteVoice() {}, matchWake: () => null,
    transcribe: async () => requests.shift(), sayLine: async text => spoken.push(text),
  });
  vm.runInContext(section('async function listenForRequest(', 'async function handleRequest(') + '\nthis.listen = listenForRequest;', context);
  assert.equal(await context.listen(() => true, 8000), 'What time is it?');
  assert.equal(recordings, 2);
  assert.equal(spoken.length, 1);
});
