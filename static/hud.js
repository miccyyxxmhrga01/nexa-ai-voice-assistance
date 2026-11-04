"use strict";

/* The display is opt-in. Model output is content, never executable HTML. */
(() => {
  function intent(text) {
    const request = text.trim().replace(/[.!?]+$/, "")
      .replace(/^(?:(?:can|could|would|will) you|(?:i'd|i would) like you to)\s+/i, "");
    if (/\b(?:don't|do not|never)\s+(?:show|display|put)\b/i.test(request)) return "none";
    if (/^(?:(?:please|okay|ok)\s*[, ]\s*)?(?:hide|clear|dismiss|close|remove)\s+(?:(?:the|this|that|my)\s+)?(?:display|screen|panel|hud|results?|visuals?)(?:\s+please)?$/i.test(request)) return "hide";
    if (/^(?:please\s+)?(?:hide|clear|dismiss|close)\s+(?:it|that|this)(?:\s+please)?$/i.test(request)) return "hide";
    if (/^(?:show|display|put up)\s+(?:me\s+)?(?:that|it|your (?:last )?(?:answer|response))(?:\s+(?:on(?: the)? screen|again|please))*$/i.test(request)) return "recall";
    if (/^(?:show|display)\s+(?:me\s+)?(?:the\s+)?desktop$/i.test(request)) return "none";
    if (/^(?:please\s+)?(?:show(?:\s+me)?|display|visuali[sz]e|put up|bring up|let me see)\s+\S/i.test(request)
      || /^(?:please\s+)?(?:put|bring|project|present|draw|write|explain|compare|summari[sz]e|list)\b.+\bon (?:the )?(?:screen|display)\b/i.test(request)) return "show";
    return "none";
  }

  function isClockRequest(text) {
    const request = text.trim().replace(/[.!?]+$/, "")
      .replace(/^(?:(?:can|could|would|will) you)\s+/i, "");
    return /^(?:please\s+)?(?:show(?:\s+me)?|display|bring up|put up|let me see)\s+(?:(?:the|a)\s+)?(?:(?:current|local)\s+)?(?:clock|time(?:\s+and\s+(?:the\s+)?date)?)(?:\s+(?:right now|now|please|for me|on (?:the )?screen))?$/i.test(request);
  }

  const escape = text => String(text).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const safeUrl = value => {
    try { return ["http:", "https:"].includes(new URL(value).protocol); } catch { return false; }
  };

  function inline(text) {
    return escape(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function renderMarkdown(content) {
    const lines = String(content).replace(/\r/g, "").split("\n");
    const output = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index++];
      if (!line.trim()) continue;
      const fence = line.match(/^\s*```([\w+#.-]*)/);
      if (fence) {
        const code = [];
        while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index++;
        output.push(`<figure class="hud-code"><figcaption>${escape(fence[1] || "Code")}</figcaption><pre tabindex="0"><code>${escape(code.join("\n"))}</code></pre></figure>`);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) { output.push(`<h${Math.min(heading[1].length + 1, 4)}>${inline(heading[2])}</h${Math.min(heading[1].length + 1, 4)}>`); continue; }
      if (index < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index])) {
        const cells = row => row.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
        const headers = cells(line);
        index++;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(cells(lines[index++]));
        output.push(`<div class="hud-table" tabindex="0"><table><thead><tr>${headers.map(cell => `<th scope="col">${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${headers.map((_, column) => `<td>${inline(row[column] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const item = line.match(/^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$/);
      if (item) {
        const ordered = !!item[2], items = [item[3]];
        while (index < lines.length) {
          const next = lines[index].match(ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/);
          if (!next) break;
          items.push(next[1]); index++;
        }
        const tag = ordered ? "ol" : "ul";
        output.push(`<${tag}>${items.map(text => `<li>${inline(text)}</li>`).join("")}</${tag}>`);
        continue;
      }
      output.push(`<p>${inline(line)}</p>`);
    }
    return output.join("");
  }

  function documentParts(content, fallback) {
    const text = String(content).slice(0, 60000).trim();
    const heading = text.match(/^#\s+([^\n]+)\n?/);
    return { title: heading ? heading[1] : fallback, content: heading ? text.slice(heading[0].length) : text };
  }

  class Controller {
    constructor(elements, onLayout) {
      this.els = elements;
      this.onLayout = onLayout;
      this.open = false;
      this.generation = 0;
      this.last = null;
      this.clockTimer = 0;
      elements.close.addEventListener("click", () => this.hide());
    }

    begin(request) {
      this.generation++;
      clearInterval(this.clockTimer);
      this.clockTimer = 0;
      this.request = request;
      this.open = true;
      this.els.panel.hidden = false;
      this.els.panel.dataset.state = "loading";
      this.els.panel.dataset.kind = "";
      this.els.panel.setAttribute("aria-busy", "true");
      this.els.title.textContent = "Bringing it into view";
      this.els.body.replaceChildren();
      this.els.sources.replaceChildren();
      this.els.sources.hidden = true;
      this.els.status.textContent = "Preparing your display";
      this.onLayout(true);
      return this.generation;
    }

    show(content, sources = [], generation = this.generation) {
      if (!this.open || generation !== this.generation) return;
      const document = documentParts(content, this.request || "Here's what you asked for");
      this.last = { content, sources };
      this.els.title.textContent = document.title;
      this.els.body.innerHTML = renderMarkdown(document.content);
      this.els.body.scrollTop = 0;
      this.els.panel.dataset.state = "ready";
      this.els.panel.setAttribute("aria-busy", "false");
      this.els.status.textContent = "On screen";
      this.sources(sources);
    }

    sources(sources) {
      const valid = sources.filter(source => safeUrl(source.url)).slice(0, 6);
      this.els.sources.innerHTML = valid.map(source => `<a href="${escape(source.url)}" target="_blank" rel="noopener noreferrer">${escape(source.title || new URL(source.url).hostname)}</a>`).join("");
      this.els.sources.hidden = !valid.length;
    }

    status(message, generation = this.generation) {
      if (this.open && generation === this.generation) this.els.status.textContent = message;
    }

    error(message, generation = this.generation) {
      if (!this.open || generation !== this.generation) return;
      this.els.panel.dataset.state = "error";
      this.els.panel.setAttribute("aria-busy", "false");
      this.els.title.textContent = "Couldn't bring that up";
      this.els.body.textContent = message;
      this.els.status.textContent = "Try asking again";
    }

    clock() {
      const update = () => {
        if (!this.open) return;
        const now = new Date();
        this.show(`# Local time\n${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}\n${now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" })}`);
        this.els.panel.dataset.kind = "clock";
      };
      update();
      this.clockTimer = setInterval(update, 1000);
    }

    hide() {
      this.generation++;
      this.open = false;
      clearInterval(this.clockTimer);
      this.clockTimer = 0;
      this.els.panel.hidden = true;
      this.els.panel.dataset.kind = "";
      this.onLayout(false);
    }
  }

  const api = { intent, isClockRequest, renderMarkdown, documentParts, Controller };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.NexaHUD = api;
})();
