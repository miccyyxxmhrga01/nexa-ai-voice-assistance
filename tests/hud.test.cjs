const { test } = require('node:test');
const assert = require('node:assert/strict');
const { intent, isClockRequest, renderMarkdown, Controller } = require('../static/hud.js');

test('ordinary conversation and PC commands do not open the HUD', () => {
  for (const text of ['How are you?', 'What is the weather?', 'Tell me a joke',
    'What does display mean?', 'What does on screen mean?', 'I saw it on the display',
    'Why is my text on the screen?', 'Show the desktop', "Don't show anything", 'Open Spotify']) {
    assert.equal(intent(text), 'none', text);
  }
});

test('explicit requests show, recall, or dismiss content', () => {
  for (const text of ['Show me the forecast', 'Display a comparison table', 'Put up my plan',
    'Please show me Python code', 'Can you show me the weather?', 'Could you display my plan',
    'Put that explanation on screen']) assert.equal(intent(text), 'show', text);
  for (const text of ['Show that', 'Display it again', 'Show your last answer']) assert.equal(intent(text), 'recall', text);
  for (const text of ['Hide the display', 'Clear the screen', 'Dismiss this panel', 'Hide that']) assert.equal(intent(text), 'hide', text);
});

test('screen content supports tables and code while escaping HTML', () => {
  const output = renderMarkdown('# Compare\n\n| Option | Cost |\n|---|---|\n| A | 10 |\n\n```html\n<script>alert(1)</script>\n```\n<img src=x onerror=alert(1)>');
  assert.match(output, /<table>/);
  assert.match(output, /<th scope="col">Option/);
  assert.match(output, /&lt;script&gt;/);
  assert.match(output, /&lt;img/);
  assert.doesNotMatch(output, /<script>|<img/);
});

test('live clock shortcut only handles actual clock requests', () => {
  for (const text of ['Show me the current time', 'Show a clock', 'Can you display the local time?', 'Show time and date']) {
    assert.equal(isClockRequest(text), true, text);
  }
  for (const text of ['Show me the time complexity of bubble sort', 'Show me how to build a clock in Python',
    'Show a comparison of clock speeds', 'Show me the time in Tokyo']) assert.equal(isClockRequest(text), false, text);
});

function controller() {
  const elements = Object.fromEntries(['panel', 'title', 'body', 'status', 'sources', 'close'].map(key => [key, {
    hidden: key === 'panel', dataset: {}, setAttribute() {},
    addEventListener(event, callback) { this.click = callback; },
    replaceChildren() { this.innerHTML = ''; },
  }]));
  const layout = [];
  return { hud: new Controller(elements, open => layout.push(open)), elements, layout };
}

test('opening docks the orb; closing prevents a late reply from reopening it', () => {
  const { hud, elements, layout } = controller();
  const first = hud.begin('Show me weather');
  hud.hide();
  hud.show('# Weather\nSunny', [], first);
  assert.equal(elements.panel.hidden, true);
  assert.deepEqual(layout, [true, false]);
  assert.equal(elements.body.innerHTML, '');
  const next = hud.begin('Show me a plan');
  hud.show('# Old answer\nWrong', [], first);
  assert.equal(elements.title.textContent, 'Bringing it into view');
  hud.show('# Your plan\nA real answer', [], next);
  assert.equal(elements.title.textContent, 'Your plan');
  assert.match(elements.body.innerHTML, /A real answer/);
});

test('sources reject executable and malformed URLs', () => {
  const { hud, elements } = controller();
  hud.begin('Show sources');
  hud.show('# Sources\nAnswer', [{ url: 'javascript:alert(1)' }, { url: 'http://' },
    { url: 'https://example.com', title: '<script>unsafe</script>' }]);
  assert.doesNotMatch(elements.sources.innerHTML, /javascript:|<script>/);
  assert.match(elements.sources.innerHTML, /https:\/\/example.com/);
  assert.match(elements.sources.innerHTML, /&lt;script&gt;/);
});
