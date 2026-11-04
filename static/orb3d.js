/* Nexa's orb: a fluid luminous center, with waves moving a volume of teal particles.
   Geometry is seeded once; each frame projects the same cloud with travelling waves and voice
   deformation. WebGL bloom supplies the haze. No libraries or network assets are needed. */

const ORB_PALETTES = {
  // On dark backgrounds (the voice session, the dark page): light that adds up toward white.
  dark: { main: [0.4, 0.83, 0.75], hot: [0.8, 1.0, 0.93], cyan: [0.35, 0.86, 0.81] },
  // On the light page: deeper greens, drawn over the page instead of added to it.
  light: { main: [0.05, 0.56, 0.33], hot: [0.02, 0.38, 0.2], cyan: [0.03, 0.52, 0.48] },
};

const ORB_GLSL = {
  quad: "attribute vec2 a; varying vec2 vUv; void main() { vUv = a * 0.5 + 0.5; gl_Position = vec4(a, 0.0, 1.0); }",

  // Charcoal-green space, diffuse light in the volume, and a faint perspective floor.
  backdrop: `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform vec2 uRes;
uniform vec2 uCenter;
uniform float uUnit;
uniform float uTime;
uniform vec4 uMood;
uniform float uIntro;
void main() {
  vec2 p = (gl_FragCoord.xy - uCenter) / uUnit;
  float d2 = dot(p, p);
  float energy = 0.3 * uMood.y + 0.2 * uMood.z + uMood.x * uMood.w;
  vec3 col = vec3(0.067, 0.091, 0.083);
  col += vec3(0.025, 0.043, 0.036) * exp(-d2 * 0.32);
  float mist = 0.92 + 0.08 * sin(p.x * 3.0 + uTime * 0.08) * sin(p.y * 4.0 - uTime * 0.06);
  col += vec3(0.065, 0.16, 0.13) * exp(-d2 * 1.5) * mist * (0.55 + energy * 0.35) * uIntro;
  col += vec3(0.055, 0.12, 0.09) * exp(-d2 * 12.0) * (0.3 + energy * 0.3) * uIntro;

  float floorHeight = uRes.y * 0.14;
  float below = (floorHeight - gl_FragCoord.y) / floorHeight;
  if (below > 0.0) {
    float z = 1.0 / (below + 0.12);
    float x = (gl_FragCoord.x - uRes.x * 0.5) / uRes.y * z * 5.0;
    float across = abs(fract(x + 0.5) - 0.5);
    float back = abs(fract(z * 2.0 + 0.5) - 0.5);
    float lines = max(1.0 - smoothstep(0.0, 0.018 * z, across), 1.0 - smoothstep(0.0, 0.028 * z, back));
    float fade = smoothstep(0.0, 0.22, below) * (1.0 - below * 0.7);
    col += vec3(0.055, 0.12, 0.1) * lines * fade * 0.18;
  }
  vec2 v = gl_FragCoord.xy / uRes - 0.5;
  col *= 1.0 - dot(v, v) * 0.55;
  gl_FragColor = vec4(col, 1.0);
}`,

  // The fluid center and its outward wave crests share the particle field's phase and speed.
  core: `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform vec2 uCenter;
uniform float uUnit;
uniform float uTime;
uniform float uIntro;
uniform float uRadius;
uniform float uMotion;
uniform float uEnergy;
uniform vec3 uTint;
uniform vec4 uWave;
void main() {
  vec2 p = (gl_FragCoord.xy - uCenter) / uUnit;
  float d = length(p);
  if (d > 1.2) { gl_FragColor = vec4(0.0); return; }
  float angle = atan(p.y, p.x);
  float phase = -uTime * uWave.y * uWave.z;
  float swell = (sin(angle * 3.0 + phase * 0.45) * 0.008 + sin(angle * 5.0 - phase * 0.7) * 0.003) * uMotion;
  float radius = uRadius + swell;
  float sd = d - radius;
  float body = 1.0 - smoothstep(-0.004, 0.006, sd);
  vec2 q = p / radius;
  float z = sqrt(max(0.0, 1.0 - dot(q, q)));
  vec3 normal = normalize(vec3(q, max(0.05, z)));
  float light = max(0.0, dot(normal, normalize(vec3(-0.4, 0.55, 0.8))));
  float flow = sin(q.x * 5.0 + q.y * 3.0 + sin(q.y * 4.0 - uTime * 0.6)) * sin(q.y * 6.0 - uTime * 0.8);
  float rim = exp(-abs(sd) * 190.0);
  vec3 surface = uTint * (0.15 + light * 0.44 + z * 0.12 + flow * 0.045);
  surface += vec3(0.32, 0.55, 0.47) * pow(light, 16.0) * 0.42;
  surface += uTint * rim * 0.38;
  float halo = exp(-abs(sd) * 18.0) * 0.075;
  float distance = max(0.0, d - uWave.x);
  float crest = pow(0.5 + 0.5 * sin(distance * uWave.y + phase), 14.0);
  float outward = smoothstep(uWave.x + 0.02, uWave.x + 0.08, d) * (1.0 - smoothstep(0.72, 1.12, d));
  float ripples = crest * outward * 0.035 * uMotion * (1.0 + uEnergy);
  vec3 color = surface * body + uTint * (halo + ripples);
  float alpha = body * 0.97 + halo + ripples;
  gl_FragColor = vec4(color * uIntro, min(1.0, alpha) * uIntro);
}`,

  // Threads are soft quads: a thin bright core with a wide glow around it.
  lineVert: `
attribute vec2 aPos;
attribute vec2 aSide;
attribute vec4 aColor;
uniform vec2 uRes;
varying float vAcross;
varying float vHW;
varying vec4 vColor;
void main() {
  vAcross = aSide.x;
  vHW = aSide.y;
  vColor = aColor;
  gl_Position = vec4(aPos / uRes * 2.0 - 1.0, 0.0, 1.0);
}`,
  lineFrag: `
precision mediump float;
uniform float uGlow;
varying float vAcross;
varying float vHW;
varying vec4 vColor;
void main() {
  float x = abs(vAcross) * vHW;
  float cw = max(vHW * 0.24, 0.6);
  float core = exp(-(x * x) / (cw * cw));
  float g = x / (vHW * 0.45);
  float a = vColor.a * (core + exp(-g * g) * uGlow);
  gl_FragColor = vec4(vColor.rgb * a, a);
}`,

  // Soft particles with a small luminous center.
  dotVert: `
attribute vec2 aPos;
attribute vec2 aSide;
attribute vec4 aColor;
uniform vec2 uRes;
varying vec4 vColor;
void main() {
  vColor = aColor;
  gl_PointSize = aSide.x;
  gl_Position = vec4(aPos / uRes * 2.0 - 1.0, 0.0, 1.0);
}`,
  dotFrag: `
precision mediump float;
varying vec4 vColor;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float d = length(c) * 2.0;
  float a = exp(-d * d * 6.0) + (1.0 - smoothstep(0.0, 0.3, d)) * 0.8;
  a *= vColor.a;
  gl_FragColor = vec4(vColor.rgb * a, a);
}`,

  // Bloom: keep the bright parts, blur them, add them back over the scene.
  bright: `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uTex;
uniform float uCut;
void main() {
  vec3 c = texture2D(uTex, vUv).rgb;
  gl_FragColor = vec4(max(c - uCut, 0.0) / (1.0 - uCut), 1.0);
}`,
  blur: `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uStep;
void main() {
  vec3 c = texture2D(uTex, vUv).rgb * 0.227027;
  c += (texture2D(uTex, vUv + uStep * 1.3846154).rgb + texture2D(uTex, vUv - uStep * 1.3846154).rgb) * 0.3162162;
  c += (texture2D(uTex, vUv + uStep * 3.2307692).rgb + texture2D(uTex, vUv - uStep * 3.2307692).rgb) * 0.0702703;
  gl_FragColor = vec4(c, 1.0);
}`,
  composite: `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform float uAmount;
void main() {
  gl_FragColor = vec4(texture2D(uScene, vUv).rgb + texture2D(uBloom, vUv).rgb * uAmount, 1.0);
}`,
};

class Orb3D {
  /** True if this PC can draw the 3D orb (WebGL works and the shaders compile). */
  static probe() {
    try {
      const gl = document.createElement("canvas").getContext("webgl");
      if (!gl) return false;
      Orb3D.compile(gl);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
      return true;
    } catch (e) {
      Orb3D.problem = String(e.message || e).slice(0, 300); // reported to the log by app.js
      return false;
    }
  }

  static compile(gl) {
    const shader = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader failed");
      return s;
    };
    const link = (vert, frag) => {
      const program = gl.createProgram();
      gl.attachShader(program, shader(gl.VERTEX_SHADER, vert));
      gl.attachShader(program, shader(gl.FRAGMENT_SHADER, frag));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "link failed");
      return program;
    };
    const G = ORB_GLSL;
    return {
      backdrop: link(G.quad, G.backdrop),
      core: link(G.quad, G.core),
      line: link(G.lineVert, G.lineFrag),
      dot: link(G.dotVert, G.dotFrag),
      bright: link(G.quad, G.bright),
      blur: link(G.quad, G.blur),
      composite: link(G.quad, G.composite),
    };
  }

  /**
   * compact: the small orb on the welcome screen (drawn over the page, no background).
   * anchor: the element the orb sits on (the canvas itself if not given).
   * world: the full-screen voice-session view (charcoal-green haze and bloom).
   */
  constructor(canvas, { compact = false, anchor = null, world = false } = {}) {
    this.canvas = canvas;
    this.compact = compact;
    this.anchor = anchor || canvas;
    this.world = world;
    this.mode = "idle";
    this.level = 0;
    this.input = 0;
    this.t = 0;
    this.listenAmt = 0;
    this.thinkAmt = 0;
    this.speakAmt = 0;
    this.intro = 1;
    this.queued = []; // ripples asked for by pulse()
    this.waveSource = null; // () => Float32Array of recent audio samples, or null
    this.running = false;
    this.quality = 1; // lowered automatically if the PC can't keep up
    this.slowFrames = 0;
    this.pointer = { x: 0, y: 0, tx: 0, ty: 0 };
    this.waveBins = new Float32Array(64);
    this.wavePeak = 0.05;
    this.roll = 0;
    this.prevLevel = 0;
    this.sinceRipple = 0;
    this.darkPage = matchMedia("(prefers-color-scheme: dark)");
    this.frame = this.frame.bind(this);
    this.initGL();
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); this.lost = true; });
    canvas.addEventListener("webglcontextrestored", () => { this.lost = false; this.initGL(); this.resize(); });
    new ResizeObserver(() => this.resize()).observe(canvas);
    if (this.anchor !== canvas) new ResizeObserver(() => this.place()).observe(this.anchor);
    this.anchor.addEventListener("pointermove", (event) => {
      if (REDUCED || event.pointerType === "touch") return;
      const rect = this.anchor.getBoundingClientRect();
      this.pointer.tx = ((event.clientX - rect.left) / rect.width - 0.5) * 0.16;
      this.pointer.ty = ((event.clientY - rect.top) / rect.height - 0.5) * 0.12;
    });
    this.anchor.addEventListener("pointerleave", () => { this.pointer.tx = this.pointer.ty = 0; });
  }

  initGL() {
    const gl = this.gl = this.canvas.getContext("webgl", { alpha: !this.world, premultipliedAlpha: true, antialias: false });
    if (!gl) throw new Error("WebGL unavailable");
    const progs = this.progs = Orb3D.compile(gl);
    const uniforms = (p, names) => Object.fromEntries(names.map((n) => [n, gl.getUniformLocation(p, n)]));
    this.loc = {
      quad: Object.fromEntries(["backdrop", "core", "bright", "blur", "composite"].map((k) => [k, gl.getAttribLocation(progs[k], "a")])),
      line: ["aPos", "aSide", "aColor"].map((n) => gl.getAttribLocation(progs.line, n)),
      dot: ["aPos", "aSide", "aColor"].map((n) => gl.getAttribLocation(progs.dot, n)),
    };
    this.u = {
      backdrop: uniforms(progs.backdrop, ["uRes", "uCenter", "uUnit", "uTime", "uMood", "uIntro"]),
      core: uniforms(progs.core, ["uCenter", "uUnit", "uTime", "uIntro", "uRadius", "uMotion", "uEnergy", "uTint", "uWave"]),
      line: uniforms(progs.line, ["uRes", "uGlow"]),
      dot: uniforms(progs.dot, ["uRes"]),
      bright: uniforms(progs.bright, ["uTex", "uCut"]),
      blur: uniforms(progs.blur, ["uTex", "uStep"]),
      composite: uniforms(progs.composite, ["uScene", "uBloom", "uAmount"]),
    };
    this.quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW); // one big triangle
    this.lineBuf = gl.createBuffer();
    this.dotBuf = gl.createBuffer();
    this.indexBuf = gl.createBuffer();
    this.build(!this.compact);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuf);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, this.geo.index, gl.STATIC_DRAW);
    const range = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE);
    this.maxPoint = range && range[1] ? range[1] : 64;
    this.targets = null;
  }

  /** Seed the volume and its short connections once, including preallocated frame buffers. */
  build(dense) {
    const cloud = createOrbCloud(dense ? 2200 : 850);
    const N = cloud.points.length, E = cloud.edges.length;
    const index = new Uint16Array(E * 6);
    for (let i = 0; i < E; i++) {
      const v = i * 4, j = i * 6;
      index.set([v, v + 1, v + 2, v + 1, v + 3, v + 2], j);
    }
    this.geo = {
      ...cloud, index, idxCount: index.length, dotCount: N,
      sx: new Float32Array(N), sy: new Float32Array(N), depth: new Float32Array(N),
      alpha: new Float32Array(N),
      lineVerts: new Float32Array(E * 4 * 8), dotVerts: new Float32Array(N * 8),
    };
    this.pulses = [];
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1;
    // The full-screen world is soft light, so it can render below screen resolution.
    const scale = this.world ? Math.min(dpr, 1.5) * 0.9 * this.quality : Math.min(dpr, 2);
    this.canvas.width = Math.max(1, Math.round(rect.width * scale));
    this.canvas.height = Math.max(1, Math.round(rect.height * scale));
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    if (this.world) this.makeTargets();
    this.place();
  }

  /** Offscreen images for the bloom: the scene at full size, and two half-size ones to blur. */
  makeTargets() {
    const gl = this.gl;
    for (const t of Object.values(this.targets || {})) {
      gl.deleteTexture(t.tex);
      gl.deleteFramebuffer(t.fb);
    }
    this.targets = null;
    const made = [];
    const make = (w, h) => {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
        [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
      const fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      const target = { tex, fb, w, h };
      made.push(target);
      return ok ? target : null;
    };
    const W = this.canvas.width, H = this.canvas.height;
    const scene = make(W, H);
    const a = make(Math.max(1, W >> 1), Math.max(1, H >> 1));
    const b = make(Math.max(1, W >> 1), Math.max(1, H >> 1));
    if (scene && a && b) this.targets = { scene, a, b };
    else for (const t of made) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); } // no bloom on this PC
  }

  /** Where the orb sits on the canvas, in canvas pixels (origin bottom-left). */
  place() {
    const cr = this.canvas.getBoundingClientRect(), ar = this.anchor.getBoundingClientRect();
    if (!cr.width || !ar.width) return;
    const sx = this.canvas.width / cr.width;
    this.cx = (ar.left + ar.width / 2 - cr.left) * sx;
    this.cy = (cr.bottom - (ar.top + ar.height / 2)) * sx;
    this.radius = Math.min(ar.width, ar.height) * 0.48 * sx;
    this.unit = this.radius * 0.94;
  }

  setMode(mode) {
    if (mode !== this.mode && (mode === "listening" || mode === "speaking")) this.pulse(0.35);
    this.mode = mode;
  }
  setLevel(v) { this.input = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0; }
  /** A wave of brightness travelling through the particles. */
  pulse(strength = 1) {
    if (!REDUCED) this.queued.push(Math.min(1, 0.3 + 0.5 * strength));
  }

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
    this.place(); // The anchor can move with a compositor-only HUD transition.
    if (!REDUCED) this.t += dt;
    const m = this.mode;
    const target = m === "speaking" ? audio.level() : m === "listening" || this.compact ? this.input : 0;
    this.level += (target - this.level) * (1 - Math.exp(-dt * (target > this.level ? 14 : 4)));
    const ease = 1 - Math.exp(-dt * 4);
    this.listenAmt += (+(m === "listening") - this.listenAmt) * ease;
    this.thinkAmt += (+(m === "thinking") - this.thinkAmt) * ease;
    this.speakAmt += (+(m === "speaking") - this.speakAmt) * ease;
    this.pointer.x += (this.pointer.tx - this.pointer.x) * ease;
    this.pointer.y += (this.pointer.ty - this.pointer.y) * ease;
    this.intro = Math.min(1, this.intro + dt / 1.3);
    // Keep the full-screen world smooth on slower graphics: render it smaller if frames run long.
    if (this.world && dt > 0.03) {
      if (++this.slowFrames > 40 && this.quality > 0.6) { this.quality = Math.max(0.6, this.quality * 0.8); this.slowFrames = 0; this.resize(); }
    } else this.slowFrames = Math.max(0, this.slowFrames - 1);
    if (!this.lost && this.unit) {
      try {
        this.draw(dt);
      } catch (e) {
        if (!this.failed && typeof report === "function") report("orb-error", { message: String(e.message || e).slice(0, 300) });
        this.failed = true;
      }
    }
    this.raf = requestAnimationFrame(this.frame);
  }

  /** The recent voice as 64 smoothed values for the particle volume. */
  readWave() {
    const wave = this.waveSource?.();
    const bins = this.waveBins, N = bins.length;
    if (!wave || wave.length < N) return false;
    const step = wave.length / N;
    let peak = 0;
    for (let i = 0; i < N; i++) {
      const i0 = Math.floor(i * step), i1 = Math.max(i0 + 1, Math.floor((i + 1) * step));
      let s = 0;
      for (let j = i0; j < i1; j++) s += wave[j] || 0;
      bins[i] += (s / (i1 - i0) - bins[i]) * 0.5;
      peak = Math.max(peak, Math.abs(bins[i]));
    }
    this.wavePeak += (Math.max(peak, 0.02) - this.wavePeak) * (peak > this.wavePeak ? 0.5 : 0.05);
    return true;
  }

  draw(dt) {
    const { gl, geo: G } = this;
    const t = this.t, L = this.level;
    const listen = this.listenAmt, think = this.thinkAmt, speak = this.speakAmt;
    const energy = 0.45 + 0.15 * listen + 0.2 * think + 0.5 * L * speak;
    const motion = REDUCED ? 0 : 1;
    this.roll += dt * motion * (0.055 + think * 0.085);
    const shown = 1 - Math.pow(1 - this.intro, 3);
    const light = this.compact && !this.darkPage.matches;
    const pal = light ? ORB_PALETTES.light : ORB_PALETTES.dark;
    const main = pal.main.map((v, i) => v + (pal.cyan[i] - v) * listen * 0.55);
    const hot = pal.hot;
    const W = this.canvas.width, H = this.canvas.height;
    const U = this.unit, cx = this.cx, cy = this.cy;
    const pixel = W / Math.max(1, this.canvas.getBoundingClientRect().width);
    const dotScale = Math.max(0.48, Math.min(1.35, U / (240 * pixel))) * pixel;

    while (this.queued.length) this.pulses.push({ age: 0, strength: this.queued.shift() });
    this.sinceRipple += dt;
    if (!REDUCED && speak > 0.3 && L > 0.45 && this.prevLevel <= 0.45 && this.sinceRipple > 0.3) {
      this.pulses.push({ age: 0, strength: L * 0.7 });
      this.sinceRipple = 0;
    }
    this.prevLevel = L;
    for (const pulse of this.pulses) pulse.age += dt;
    this.pulses = this.pulses.filter(pulse => pulse.age < 1.6);
    const hasWave = this.readWave();
    const waveGain = 1 / Math.max(0.02, this.wavePeak);
    const yaw = this.roll + this.pointer.x;
    const pitch = Math.sin(t * 0.11) * 0.07 * motion - this.pointer.y;
    const c = Math.cos(yaw), sn = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const breath = 1 + (Math.sin(t * 0.7) * 0.008 + L * (listen * 0.035 + speak * 0.065)) * motion;
    const coreRadius = orbCoreRadius(t, L * (listen + speak) * motion);
    const DV = G.dotVerts;
    let d = 0;
    for (let i = 0; i < G.points.length; i++) {
      const p = G.points[i];
      const wave = hasWave ? Math.max(-1, Math.min(1, this.waveBins[p.band] * waveGain)) : Math.sin(p.y * 9 + t * 2.5);
      const ripple = orbWaveAt(p.r, t);
      let displacement = ripple * ORB_WAVE.amplitude * (1 + L * (listen + speak));
      const deformVoice = wave * L * (listen + speak) * 0.045;
      let pulseLight = 0;
      for (const pulse of this.pulses) {
        const delta = (p.r - ORB_WAVE.core - pulse.age * 0.7) / 0.12;
        const push = Math.exp(-delta * delta) * pulse.strength * (1 - pulse.age / 1.6);
        displacement += push * 0.04;
        pulseLight += push;
      }
      const deform = breath + (displacement / Math.max(0.16, p.r) + deformVoice) * motion;
      const drift = Math.sin(t * 0.32 + p.seed * 40) * 0.009 * motion;
      const x = p.x * deform, y = p.y * deform + drift, z = p.z * deform;
      const x1 = x * c + z * sn, z1 = z * c - x * sn;
      const y1 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
      const perspective = 4.8 / (5 - z2);
      const depth = Math.max(0, Math.min(1, (z2 + 1.1) / 2.2));
      const px = cx + x1 * perspective * U, py = cy + y1 * perspective * U;
      const reveal = Math.max(0, Math.min(1, (this.intro * 1.45 - p.r * 0.4) / 0.55));
      const twinkle = 0.68 + Math.sin(t * (0.5 + p.seed * 0.8) + p.seed * 70) * 0.18;
      const screenRadius = Math.hypot(x1, y1) * perspective;
      const behindCore = screenRadius < coreRadius && z2 < Math.sqrt(Math.max(0, coreRadius * coreRadius - screenRadius * screenRadius));
      const alpha = Math.min(0.95, ((0.22 + depth * 0.6) * (twinkle + Math.max(0, ripple) * 0.2 * motion) + pulseLight * 0.5 + L * speak * 0.12) * reveal) * (behindCore ? 0.06 : 1);
      const bright = p.size > 0.82 ? 0.7 : 0.18 + p.seed * 0.3;
      const size = (2.8 + p.size * p.size * 6.8 + Math.max(0, z2) * 1.8) * dotScale * perspective;
      G.sx[i] = px; G.sy[i] = py; G.depth[i] = depth; G.alpha[i] = alpha;
      DV[d++] = px; DV[d++] = py; DV[d++] = Math.min(this.maxPoint, size); DV[d++] = 0;
      DV[d++] = main[0] + (hot[0] - main[0]) * bright;
      DV[d++] = main[1] + (hot[1] - main[1]) * bright;
      DV[d++] = main[2] + (hot[2] - main[2]) * bright;
      DV[d++] = alpha * shown;
    }

    const LV = G.lineVerts;
    let o = 0;
    const hw = Math.max(0.85, dotScale * 1.25);
    for (const edge of G.edges) {
      const a = edge.a, b = edge.b;
      const x = G.sx[a], y = G.sy[a], dx = G.sx[b] - x, dy = G.sy[b] - y;
      const length = Math.max(0.01, Math.hypot(dx, dy));
      const nx = -dy / length * hw, ny = dx / length * hw;
      const depth = (G.depth[a] + G.depth[b]) * 0.5;
      const alpha = edge.alpha * (0.15 + depth * 0.85) * Math.min(G.alpha[a], G.alpha[b]) * shown * (0.8 + think * 0.65);
      for (let v = 0; v < 4; v++) {
        const end = v >= 2, side = v % 2 === 0 ? 1 : -1;
        LV[o++] = (end ? G.sx[b] : x) + nx * side;
        LV[o++] = (end ? G.sy[b] : y) + ny * side;
        LV[o++] = side; LV[o++] = hw;
        LV[o++] = main[0]; LV[o++] = main[1]; LV[o++] = main[2]; LV[o++] = alpha;
      }
    }

    const bloom = this.world ? this.targets : null;
    gl.bindFramebuffer(gl.FRAMEBUFFER, bloom ? bloom.scene.fb : null);
    gl.viewport(0, 0, W, H);
    if (this.world) {
      gl.disable(gl.BLEND);
      this.quad("backdrop", () => {
        const u = this.u.backdrop;
        gl.uniform2f(u.uRes, W, H);
        gl.uniform2f(u.uCenter, cx, cy);
        gl.uniform1f(u.uUnit, U);
        gl.uniform1f(u.uTime, t % 3600);
        gl.uniform4f(u.uMood, L, listen, think, speak);
        gl.uniform1f(u.uIntro, shown);
      });
    } else {
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    this.quad("core", () => {
      const u = this.u.core;
      gl.uniform2f(u.uCenter, cx, cy);
      gl.uniform1f(u.uUnit, U);
      gl.uniform1f(u.uTime, t);
      gl.uniform1f(u.uIntro, shown);
      gl.uniform1f(u.uRadius, coreRadius);
      gl.uniform1f(u.uMotion, motion);
      gl.uniform1f(u.uEnergy, L * (listen + speak));
      gl.uniform3f(u.uTint, main[0], main[1], main[2]);
      gl.uniform4f(u.uWave, ORB_WAVE.core, ORB_WAVE.number, ORB_WAVE.speed, ORB_WAVE.amplitude);
    });
    if (light) gl.blendFuncSeparate(gl.ONE, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    else gl.blendFunc(gl.ONE, gl.ONE);
    const bind = (locs) => {
      for (const l of locs) gl.enableVertexAttribArray(l);
      gl.vertexAttribPointer(locs[0], 2, gl.FLOAT, false, 32, 0);
      gl.vertexAttribPointer(locs[1], 2, gl.FLOAT, false, 32, 8);
      gl.vertexAttribPointer(locs[2], 4, gl.FLOAT, false, 32, 16);
    };
    const unbind = (locs) => { for (const l of locs) gl.disableVertexAttribArray(l); };

    gl.useProgram(this.progs.line);
    gl.uniform2f(this.u.line.uRes, W, H);
    gl.uniform1f(this.u.line.uGlow, light ? 0.12 : 0.32);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.lineBuf);
    gl.bufferData(gl.ARRAY_BUFFER, LV, gl.DYNAMIC_DRAW);
    bind(this.loc.line);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indexBuf);
    gl.drawElements(gl.TRIANGLES, G.idxCount, gl.UNSIGNED_SHORT, 0);
    unbind(this.loc.line);

    gl.useProgram(this.progs.dot);
    gl.uniform2f(this.u.dot.uRes, W, H);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.dotBuf);
    gl.bufferData(gl.ARRAY_BUFFER, DV, gl.DYNAMIC_DRAW);
    bind(this.loc.dot);
    gl.drawArrays(gl.POINTS, 0, G.dotCount);
    unbind(this.loc.dot);
    gl.disable(gl.BLEND);

    if (bloom) {
      // Keep the bright parts at half size, blur them twice (wider the second time), add them back.
      const { scene, a, b } = bloom;
      const pass = (into, name, tex, setup) => {
        gl.bindFramebuffer(gl.FRAMEBUFFER, into ? into.fb : null);
        gl.viewport(0, 0, into ? into.w : W, into ? into.h : H);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, tex);
        this.quad(name, setup);
      };
      pass(a, "bright", scene.tex, () => {
        gl.uniform1i(this.u.bright.uTex, 0);
        gl.uniform1f(this.u.bright.uCut, 0.18);
      });
      for (const [from, into, sx2, sy2] of [[a, b, 1, 0], [b, a, 0, 1], [a, b, 2, 0], [b, a, 0, 2]]) {
        pass(into, "blur", from.tex, () => {
          gl.uniform1i(this.u.blur.uTex, 0);
          gl.uniform2f(this.u.blur.uStep, sx2 / from.w, sy2 / from.h);
        });
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, a.tex);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, scene.tex);
      this.quad("composite", () => {
        gl.uniform1i(this.u.composite.uScene, 0);
        gl.uniform1i(this.u.composite.uBloom, 1);
        gl.uniform1f(this.u.composite.uAmount, 0.8 + 0.3 * energy);
      });
    }
  }

  /** Draws a full-screen pass with one of the quad programs. */
  quad(name, setup) {
    const gl = this.gl, loc = this.loc.quad[name];
    gl.useProgram(this.progs[name]);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuf);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    setup();
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.disableVertexAttribArray(loc);
  }
}
