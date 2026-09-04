// The chat page. Standalone on purpose — it shares the stylesheet with the
// console and none of its behaviour, so nothing the console grows later can
// leak onto a surface meant for people who do not operate it.

// ---------------------------------------------------------------- markdown
//
// Models write markdown, so a reply set as plain text shows its own syntax:
// asterisks, hashes and backticks, all visible. This renders it — with the
// rule that model output is not trusted. A prompt-injected reply could carry
// a <script> or an onerror, so everything is escaped *first* and the only HTML
// on the page afterwards is what these rules put there. No raw passthrough,
// no images, and links only where the scheme is http(s).
//
// Hand-written rather than a library because this project has no build step,
// and a dependency for six regexes is a poor trade.

const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ESCAPES[c]);

const MARK = '\u0001';   // a character no model emits; parks code spans while
                         // emphasis is applied, then swapped back

function inlineMd(text) {
  let s = escapeHtml(text);

  // Code spans are lifted out first so their contents are never treated as
  // emphasis — `**` inside a code span is two asterisks, not bold.
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, code) => MARK + (codes.push(code) - 1) + MARK);

  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$1</a>');

  const parked = new RegExp(MARK + '(\\d+)' + MARK, 'g');
  return s.replace(parked, (_, i) => `<code>${codes[i]}</code>`);
}

function renderMd(src) {
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let para = [], list = null, fence = null;

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; }
  };
  const flushList = () => {
    if (list) {
      out.push(`<${list.tag}>`
             + list.items.map(i => `<li>${inlineMd(i)}</li>`).join('')
             + `</${list.tag}>`);
      list = null;
    }
  };

  for (const line of lines) {
    if (fence !== null) {
      if (/^\s*```/.test(line)) {
        out.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`);
        fence = null;
      } else fence.push(line);
      continue;
    }
    if (/^\s*```/.test(line)) { flushPara(); flushList(); fence = []; continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }

    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); flushList();
      // A heading inside a chat bubble is a size cue, not a document outline,
      // so they all render at one weight rather than as h1…h6.
      out.push(`<p class="md-h">${inlineMd(m[2])}</p>`);
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== 'ul') { flushList(); list = {tag: 'ul', items: []}; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== 'ol') { flushList(); list = {tag: 'ol', items: []}; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushPara(); flushList();
      out.push(`<blockquote>${inlineMd(m[1])}</blockquote>`);
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushPara(); flushList();
      out.push('<hr>');
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara(); flushList();
  if (fence !== null) out.push(`<pre><code>${escapeHtml(fence.join('\n'))}</code></pre>`);
  return out.join('');
}

// ------------------------------------------------------------------- page

// Two surfaces use this: the chat page a colleague is given, and the Jarvis
// screen an administrator builds on. They differ in where they post and what
// the thread in the address bar is called, and in nothing else — so those are
// the only two things parameterised. A second copy of the markdown renderer
// and the dedupe was the alternative, and one of the copies would have rotted.
const BASE = window.CHAT_BASE || `/chat/${encodeURIComponent(window.CHAT_AGENT)}`;
const THREAD_PARAM = window.CHAT_THREAD_PARAM || 'session';
const CAN_REPORT = window.CHAT_REPORT !== false;
const PANEL = window.CHAT_PANEL || null;

const AGENT = window.CHAT_AGENT;
let sessionId = window.CHAT_SESSION || null;
let threadId = window.CHAT_THREAD || null;
let stream = null, live = null, steps = null;

const log = document.getElementById('chat');
const form = document.getElementById('chat-form');
const input = document.getElementById('chat-input');
const sendBtn = form.querySelector('button[type=submit]');

const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
function toBottom(force) {
  if (force || atBottom()) log.scrollTop = log.scrollHeight;
}

// 24-hour, to match the times the server renders into the page for messages
// that arrived earlier. Two clocks in one conversation — 14:32 above, 2:33 PM
// below — reads as a bug even when both are right.
const timeNow = () => new Date().toLocaleTimeString('en-GB',
  {hour: '2-digit', minute: '2-digit', hour12: false});

function bubble(role, text, cls) {
  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();

  const wrap = document.createElement('div');
  wrap.className = `turn ${role}`;
  const el = document.createElement('div');
  el.className = `bubble ${role} ${cls || ''}`;
  el.textContent = text || '';
  wrap.append(el);

  const meta = document.createElement('div');
  meta.className = 'turn-meta';
  meta.append(document.createTextNode(timeNow()));
  wrap.append(meta);

  log.append(wrap);
  toBottom(true);
  return el;
}

// A finished reply is worth copying; a half-streamed one is not, so the button
// arrives with the final text.
function addCopy(el) {
  const meta = el.parentElement.querySelector('.turn-meta');
  if (!meta || meta.querySelector('.copy-reply')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'copy-reply';
  btn.textContent = 'Copy';
  btn.setAttribute('aria-label', 'Copy this reply');
  btn.addEventListener('click', async () => {
    const text = el.dataset.raw || el.textContent;
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
    } catch (err) {
      const range = document.createRange();
      range.selectNodeContents(el);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      btn.textContent = 'Selected';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
    }
  });
  meta.append(btn);
}

// "That was wrong", from the person who noticed. The conversation becomes a
// test, which is the only reliable way tests about real behaviour ever get
// written — the alternative is somebody curating them later from memory.
function addReport(el) {
  if (!CAN_REPORT) return;
  const meta = el.parentElement.querySelector('.turn-meta');
  if (!meta || meta.querySelector('.report-reply')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'report-reply';
  btn.textContent = 'Not right';
  btn.setAttribute('aria-label', 'Report this reply as wrong');
  btn.addEventListener('click', async () => {
    if (!sessionId) return;
    const note = window.prompt(
      'What should it have said, or what went wrong?\n\n'
      + 'This conversation is saved as a test, so the same mistake gets caught '
      + 'next time.') ;
    if (note === null) return;          // cancelled
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      const r = await fetch(`${BASE}/report`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({session_id: sessionId, note})
      });
      btn.textContent = r.ok ? 'Reported — thank you' : 'Could not save that';
      btn.classList.toggle('done', r.ok);
    } catch (err) {
      btn.textContent = 'Could not save that';
      btn.disabled = false;
    }
  });
  meta.append(btn);
}

// What it is doing, while it does it. Names only — never arguments, never
// results. Those belong to whoever runs the agent, and this page is not that.
function stepLine(text) {
  if (!steps) {
    const empty = document.getElementById('chat-empty');
    if (empty) empty.remove();
    steps = document.createElement('div');
    steps.className = 'steps';
    steps.setAttribute('aria-live', 'polite');
    log.append(steps);
  }
  const row = document.createElement('div');
  row.className = 'step running';
  const dot = document.createElement('span');
  dot.className = 'step-dot';
  dot.setAttribute('aria-hidden', 'true');
  row.append(dot, document.createTextNode(text));
  steps.append(row);
  toBottom();
  return row;
}

function clearSteps() {
  if (steps) steps.remove();
  steps = null;
}

function busy(on) {
  input.disabled = on;
  sendBtn.disabled = on;
  if (!on) input.focus();
}

const humanName = t => String(t || '').replace(/_/g, ' ');

// Every message event that has been put on screen, by sequence number. The
// server resumes rather than replays, and the browser resumes again by itself
// after a dropped connection — but a stream is a thing that can deliver
// something twice, and a duplicated reply is the one mistake this page cannot
// hide. Cheap insurance.
const shown = new Set();

function connect(sid) {
  if (stream) stream.close();
  const after = Number(window.CHAT_AFTER) || 0;
  stream = new EventSource(
    `${BASE}/stream/${encodeURIComponent(sid)}?after=${after}`);

  stream.addEventListener('tool.called', (e) => {
    const payload = JSON.parse(e.data).payload || {};
    stepLine(`Using ${humanName(payload.tool)}`);
  });

  stream.addEventListener('tool.result', () => {
    const rows = steps ? steps.querySelectorAll('.step.running') : [];
    const last = rows[rows.length - 1];
    if (last) last.classList.replace('running', 'done');
  });

  stream.addEventListener('model.delta', (e) => {
    const {text} = JSON.parse(e.data);
    if (!live) {
      clearSteps();
      live = bubble('agent', '');
      live.classList.add('streaming');
    }
    live.textContent += text;
    toBottom();
  });

  // The authoritative reply. Replaces whatever streamed, so a dropped delta
  // cannot leave a half-sentence on screen — and markdown is rendered once
  // here rather than on every token.
  stream.addEventListener('message.sent', (e) => {
    const data = JSON.parse(e.data);
    if (data.seq != null) {
      if (shown.has(data.seq)) return;
      shown.add(data.seq);
    }
    const text = (data.payload || {}).text || '';
    clearSteps();
    const el = live || bubble('agent', '');
    el.classList.remove('streaming');
    el.dataset.raw = text;
    el.innerHTML = renderMd(text);
    addCopy(el);
    addReport(el);
    live = null;
    busy(false);
    toBottom(true);
  });

  stream.addEventListener('approval.requested', () => {
    clearSteps();
    if (live) { live.parentElement.remove(); live = null; }
    bubble('agent', 'Waiting for someone to approve this before it can go further. '
                  + 'The answer will appear here once they have.', 'paused');
    busy(false);
  });

  stream.addEventListener('turn.completed', () => {
    clearSteps();
    busy(false);
    refreshPanel();
  });

  stream.addEventListener('error.raised', (e) => {
    const payload = JSON.parse(e.data).payload || {};
    clearSteps();
    if (live) { live.parentElement.remove(); live = null; }
    bubble('agent', payload.message || 'That did not finish. Try again in a moment.',
           'err');
    busy(false);
  });
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = '';
  autosize();
  bubble('user', text);
  busy(true);
  live = null;
  stepLine('Thinking');

  let response, body;
  try {
    response = await fetch(`${BASE}/messages`, {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({text, session_id: sessionId, chat_id: threadId})
    });
    body = await response.json();
  } catch (err) {
    clearSteps();
    bubble('agent', 'Could not reach the server. Your message was not sent.', 'err');
    busy(false);
    return;
  }
  if (!response.ok) {
    clearSteps();
    bubble('agent', body.error || 'That could not be sent.', 'err');
    busy(false);
    return;
  }
  if (!sessionId) {
    sessionId = body.session_id;
    threadId = body.thread_id || sessionId;
    history.replaceState(null, '', `?${THREAD_PARAM}=${encodeURIComponent(threadId)}`);
    connect(sessionId);
  }
});

// What it has built, refreshed when a turn ends rather than reloaded with the
// page — a panel that only updates on F5 is a panel you stop believing.
async function refreshPanel() {
  if (!PANEL) return;
  const target = document.getElementById('jarvis-panel');
  if (!target) return;
  const scroll = target.scrollTop;
  try {
    const r = await fetch(PANEL + (threadId ? `?chat=${encodeURIComponent(threadId)}` : ''));
    if (!r.ok) return;
    target.innerHTML = await r.text();
    // Rebuilt markup has none of the state the reader put there — which
    // section they folded, what they typed in the filter, where they had
    // scrolled to. The page puts it back; without this, a turn ending yanks
    // the panel out from under whoever was reading it.
    target.scrollTop = scroll;
    document.dispatchEvent(new CustomEvent('panel-refreshed'));
  } catch (err) { /* the panel is a convenience; the conversation is the page */ }
}

// Enter sends, Shift+Enter makes a new line, and the box grows to fit rather
// than scrolling a single line out of sight.
function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
}
input.addEventListener('input', autosize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

document.querySelectorAll('.opener').forEach(b => b.addEventListener('click', () => {
  input.value = b.dataset.text;
  autosize();
  form.requestSubmit();
}));

const sideToggle = document.getElementById('side-toggle');
const scrim = document.getElementById('side-scrim');
function setSide(open) {
  document.body.classList.toggle('side-open', open);
  if (sideToggle) sideToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (scrim) scrim.hidden = !open;
}
if (sideToggle) {
  sideToggle.addEventListener('click',
    () => setSide(!document.body.classList.contains('side-open')));
}
if (scrim) scrim.addEventListener('click', () => setSide(false));
addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.body.classList.contains('side-open')) {
    setSide(false);
    if (sideToggle) sideToggle.focus();
  }
});

// Replies that arrived with the page are plain text until now, for the same
// reason the streamed ones are: rendering happens in one place, over text the
// server never marked as HTML.
document.querySelectorAll('.bubble.agent[data-md]').forEach(el => {
  const raw = el.textContent;
  el.dataset.raw = raw;
  el.innerHTML = renderMd(raw);
  addCopy(el);
  addReport(el);
});

toBottom(true);
if (sessionId) connect(sessionId);
input.focus();
