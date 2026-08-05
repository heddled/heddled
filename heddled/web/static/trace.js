/* The trace view island.
 *
 * Progressive enhancement over server-rendered HTML: the timeline already
 * exists in the markup, this adds the SSE feed, the detail pane, keyboard
 * navigation and deep-link handling. No framework, no build step.
 */
(function () {
  const root = document.getElementById('trace');
  if (!root) return;

  const timeline = document.getElementById('timeline');
  const detail = document.getElementById('detail');
  const detailTitle = document.getElementById('detail-title');
  const filterInput = document.getElementById('trace-filter');
  let source = null;
  let selected = null;

  // ---------------------------------------------------------------- render

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  }

  function json(v) {
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }

  function kv(pairs) {
    return '<dl>' + pairs.filter(p => p[1] !== undefined && p[1] !== null && p[1] !== '')
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') + '</dl>';
  }

  function renderContext(p) {
    let html = kv([
      ['model', p.model], ['messages', p.message_count], ['tools', p.tool_count],
      ['iteration', p.iteration]
    ]);
    if (p.pruned) {
      html += `<p class="muted">Full context pruned by retention (${esc(p.pruned_reason || '')}).</p>`;
      return html;
    }
    if (p.system) {
      html += `<div class="ctx-msg"><div class="role">system</div><pre>${esc(p.system)}</pre></div>`;
    }
    (p.messages || []).forEach(m => {
      let body = m.content || '';
      if (m.tool_calls && m.tool_calls.length) body += '\n' + json(m.tool_calls);
      const role = m.role + (m.name ? ` · ${m.name}` : '');
      html += `<div class="ctx-msg"><div class="role">${esc(role)}</div><pre>${esc(body)}</pre></div>`;
    });
    if (p.tools && p.tools.length) {
      html += `<h3>Tool schemas sent</h3><pre class="payload">${esc(json(p.tools))}</pre>`;
    }
    return html;
  }

  function renderDetail(type, payload) {
    const p = payload || {};
    let html = '';
    switch (type) {
      case 'context.built':
        html = renderContext(p);
        break;
      case 'model.responded':
        html = kv([
          ['model', p.model], ['stop reason', p.stop_reason],
          ['duration', p.duration_ms != null ? p.duration_ms + ' ms' : null],
          ['input tokens', (p.usage || {}).input_tokens],
          ['output tokens', (p.usage || {}).output_tokens],
          ['cost', p.cost_eur ? '€' + p.cost_eur : null]
        ]);
        if (p.text) html += `<h3>Text</h3><pre class="payload">${esc(p.text)}</pre>`;
        if (p.tool_calls && p.tool_calls.length)
          html += `<h3>Tool calls</h3><pre class="payload">${esc(json(p.tool_calls))}</pre>`;
        break;
      case 'tool.called':
        html = kv([['tool', p.tool], ['call id', p.call_id], ['mocked', p.mocked || false]]);
        html += `<h3>Arguments</h3><pre class="payload">${esc(json(p.arguments))}</pre>`;
        break;
      case 'tool.result':
        html = kv([
          ['tool', p.tool],
          ['duration', p.duration_ms != null ? p.duration_ms + ' ms' : null],
          ['error', p.error ? 'yes' : 'no'], ['mocked', p.mocked || false]
        ]);
        html += `<h3>Result</h3><pre class="payload${p.error ? ' err' : ''}">${esc(
          typeof p.result === 'string' ? p.result : json(p.result))}</pre>`;
        break;
      case 'approval.requested':
        html = kv([['tool', p.tool], ['routed to', p.routed_to], ['reason', p.reason],
                   ['approval id', p.approval_id]]);
        html += `<h3>Proposed arguments</h3><pre class="payload">${esc(json(p.arguments))}</pre>`;
        if (p.delivery && p.delivery.approve_url) {
          html += `<div class="row"><a class="btn tiny ok" href="${esc(p.delivery.approve_url)}">Approve</a>` +
                  `<a class="btn tiny bad" href="${esc(p.delivery.deny_url)}">Deny</a></div>`;
        }
        break;
      case 'error.raised':
        html = kv([['kind', p.kind], ['tool', p.tool]]);
        html += `<pre class="payload err">${esc(p.message || '')}</pre>`;
        if (p.trace) html += `<pre class="payload">${esc(p.trace)}</pre>`;
        break;
      default:
        html = `<pre class="payload">${esc(json(p))}</pre>`;
    }
    return html;
  }

  function select(li, push) {
    if (!li) return;
    if (selected) selected.classList.remove('sel');
    selected = li;
    li.classList.add('sel');
    li.scrollIntoView({block: 'nearest'});
    const payload = JSON.parse(li.dataset.payload || '{}');
    detailTitle.textContent = `${li.dataset.type} · #${li.dataset.seq}`;
    detail.innerHTML = renderDetail(li.dataset.type, payload);
    if (push !== false && li.id) history.replaceState(null, '', '#' + li.id);
  }

  timeline.addEventListener('click', e => {
    const li = e.target.closest('li.ev');
    if (li) select(li);
  });

  // ------------------------------------------------------------- keyboard

  document.addEventListener('keydown', e => {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
    const items = [...timeline.querySelectorAll('li.ev')].filter(l => l.style.display !== 'none');
    if (!items.length) return;
    const i = selected ? items.indexOf(selected) : -1;
    if (e.key === 'j') { e.preventDefault(); select(items[Math.min(i + 1, items.length - 1)]); }
    else if (e.key === 'k') { e.preventDefault(); select(items[Math.max(i - 1, 0)]); }
    else if (e.key === 'Enter' && selected) {
      e.preventDefault();
      detail.querySelectorAll('pre.payload').forEach(p => {
        p.style.maxHeight = p.style.maxHeight === 'none' ? '' : 'none';
      });
    } else if (e.key === 'g') { select(items[0]); }
    else if (e.key === 'G') { select(items[items.length - 1]); }
  });

  // --------------------------------------------------------------- filter

  if (filterInput) {
    filterInput.addEventListener('input', () => {
      const q = filterInput.value.toLowerCase();
      let shown = 0;
      timeline.querySelectorAll('li.ev').forEach(li => {
        const hit = !q || li.textContent.toLowerCase().includes(q);
        li.style.display = hit ? '' : 'none';
        if (hit) shown++;
      });
      // Filtering everything away used to leave a blank column with no hint
      // that anything had been hidden.
      let note = document.getElementById('trace-no-match');
      if (!shown && q) {
        if (!note) {
          note = document.createElement('p');
          note.id = 'trace-no-match';
          note.className = 'pane-empty';
          timeline.after(note);
        }
        note.textContent = `No step matches "${filterInput.value}". `
          + `${timeline.querySelectorAll('li.ev').length} are hidden.`;
      } else if (note) {
        note.remove();
      }
    });
  }

  // ------------------------------------------------------------------ SSE

  function appendEvent(data) {
    if (timeline.querySelector(`li[data-seq="${data.seq}"]`)) return;
    // "Each step appears here as it happens" stayed on screen above the steps
    // once they started appearing.
    const placeholder = document.getElementById('timeline-empty');
    if (placeholder) placeholder.remove();
    const li = document.createElement('li');
    li.className = `ev ${data.css || ''} new`;
    li.id = 'e-' + data.seq;
    li.dataset.seq = data.seq;
    li.dataset.type = data.type;
    li.dataset.payload = JSON.stringify(data.payload || {});
    li.dataset.ts = data.ts;
    const clock = new Date((data.ts || 0) * 1000).toTimeString().slice(0, 8);
    li.innerHTML =
      `<span class="ev-seq">${data.seq}</span>` +
      `<span class="ev-type">${esc(data.type)}</span>` +
      `<span class="ev-summary">${esc(data.summary || '')}</span>` +
      `<span class="ev-time">${clock}</span>`;
    const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 60;
    timeline.append(li);
    if (atBottom) li.scrollIntoView({block: 'nearest'});
    setTimeout(() => li.classList.remove('new'), 1000);
  }

  function connect(url) {
    if (!url) return;
    disconnect();
    const last = [...timeline.querySelectorAll('li.ev')].pop();
    const after = last ? last.dataset.seq : 0;
    source = new EventSource(`${url}${url.includes('?') ? '&' : '?'}after=${after}`);
    source.onmessage = e => appendEvent(JSON.parse(e.data));
    // Named events (the contract's types) arrive with `event:` set, so listen broadly.
    ['trigger.fired', 'message.received', 'context.built', 'model.invoked',
     'model.responded', 'tool.called', 'tool.result', 'approval.requested',
     'approval.resolved', 'operator.injected', 'message.sent', 'turn.completed',
     'error.raised'].forEach(t => {
      source.addEventListener(t, e => appendEvent(JSON.parse(e.data)));
    });
    // A dropped stream used to be invisible: the dot went on saying "live"
    // while nothing more arrived. EventSource reconnects by itself, so this
    // reports the gap rather than trying to fix it.
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
  }

  function setLive(on) {
    const dot = document.querySelector('.live-dot');
    if (!dot) return;
    dot.classList.toggle('lost', !on);
    dot.title = on ? 'streaming live' : 'connection lost — trying again';
    const word = dot.nextSibling;
    if (word && word.nodeType === Node.TEXT_NODE) {
      word.textContent = on ? 'live' : 'reconnecting…';
    }
  }

  function disconnect() {
    if (source) { source.close(); source = null; }
  }

  window.HeddledTrace = {connect, disconnect, appendEvent};

  if (root.dataset.live === '1' && root.dataset.stream) connect(root.dataset.stream);

  // ------------------------------------------------------------ deep link

  if (location.hash.startsWith('#e-')) {
    const li = document.querySelector(location.hash.replace(/[^#\w-]/g, ''));
    if (li) select(li, false);
  } else {
    const first = timeline.querySelector('li.ev');
    if (first) select(first, false);
  }
})();
