// The Jarvis screen's two side panes.
//
// Both are rendered by the server and swapped whole when a turn ends — the
// alternative was building the same lists twice, once in Jinja and once here,
// and having them disagree within a week. What this file owns is the state the
// markup cannot carry across a swap: which sections the reader folded, what
// they typed in the filter, where they had scrolled to.
//
// Listeners are delegated from the two containers, which survive the swap.

(() => {
  const rail = document.getElementById('jarvis-rail');
  const bench = document.getElementById('jarvis-bench');
  if (!rail) return;

  let query = '';

  const remember = (name, open) => {
    try { localStorage.setItem('jarvis-panel:' + name, open ? '1' : '0'); }
    catch (err) { /* private window, or site data blocked. Not worth a fuss. */ }
  };
  const recall = (name) => {
    try { return localStorage.getItem('jarvis-panel:' + name); }
    catch (err) { return null; }
  };

  // ------------------------------------------------------------- the filter

  function applyFilter() {
    const needle = query.trim().toLowerCase();
    let total = 0;
    rail.querySelectorAll('details.panel-group').forEach(group => {
      let shown = 0;
      group.querySelectorAll('.panel-item').forEach(item => {
        const hit = !needle
          || (item.dataset.find || '').toLowerCase().includes(needle);
        item.hidden = !hit;
        if (hit) shown++;
      });
      total += shown;
      const count = group.querySelector('.panel-count');
      if (count) {
        const all = count.dataset.count;
        count.textContent = needle && shown !== Number(all)
          ? `${shown} of ${all}` : all;
      }
      // Prose explaining a section is noise while you are hunting for one named
      // thing, and "None yet" is a lie when the truth is "none matching that".
      group.querySelectorAll('.hint, .panel-empty, .rail-new').forEach(
        el => { el.hidden = !!needle; });
      if (needle) {
        group.hidden = shown === 0;
        group.open = true;
      } else {
        group.hidden = false;
        group.open = recall(group.dataset.panel) !== '0';
      }
    });
    const none = document.getElementById('panel-none');
    if (none) none.hidden = !(query.trim() && total === 0);
  }

  function restoreRail() {
    rail.querySelectorAll('details.panel-group').forEach(group => {
      const saved = recall(group.dataset.panel);
      if (saved !== null) group.open = saved === '1';
    });
    const box = document.getElementById('panel-filter');
    if (box) box.value = query;
    applyFilter();
  }

  rail.addEventListener('input', (e) => {
    if (e.target.id !== 'panel-filter') return;
    query = e.target.value;
    applyFilter();
  });
  rail.addEventListener('toggle', (e) => {
    // Only a person's own toggle is a preference; the ones the filter forces
    // open are not.
    const group = e.target.closest('details.panel-group');
    if (group && !query.trim()) remember(group.dataset.panel, group.open);
  }, true);
  rail.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.target.id === 'panel-filter') {
      e.target.value = ''; query = ''; applyFilter();
    }
  });

  // ------------------------------------------------------------ the bench

  function stickTerminal() {
    const term = document.getElementById('term');
    // A terminal that does not follow its own output is a terminal you scroll
    // manually after every command.
    if (term) term.scrollTop = term.scrollHeight;
  }

  async function refreshBench() {
    const body = document.getElementById('bench-body');
    if (!body || !window.CHAT_BENCH) return;
    const params = new URLSearchParams({ tab: window.CHAT_BENCH_TAB || 'files' });
    if (window.CHAT_THREAD) params.set('chat', window.CHAT_THREAD);
    try {
      const r = await fetch(`${window.CHAT_BENCH}?${params}`);
      if (!r.ok) return;
      body.innerHTML = await r.text();
      stickTerminal();
    } catch (err) { /* the bench is a view; the conversation is the page */ }
  }

  // ---------------------------------------------------------- folding away

  const grid = document.querySelector('.jarvis-grid');

  function setPane(which, open) {
    const pane = which === 'rail' ? rail : bench;
    if (!pane || !grid) return;
    pane.classList.toggle('shut', !open);
    grid.classList.toggle(`${which}-shut`, !open);
    const button = pane.querySelector('.pane-toggle');
    if (button) button.setAttribute('aria-expanded', open ? 'true' : 'false');
    try { localStorage.setItem(`jarvis-pane:${which}`, open ? '1' : '0'); }
    catch (err) { /* nothing worth failing over */ }
  }

  document.querySelectorAll('.pane-toggle').forEach(button => {
    button.addEventListener('click', () => {
      const which = button.dataset.pane;
      const pane = which === 'rail' ? rail : bench;
      setPane(which, pane.classList.contains('shut'));
    });
  });

  ['rail', 'bench'].forEach(which => {
    let saved = null;
    try { saved = localStorage.getItem(`jarvis-pane:${which}`); } catch (err) { /* */ }
    if (saved === '0') setPane(which, false);
  });

  // ------------------------------------------------------------- refreshing
  //
  // A turn ending is the moment both panes go stale: it may have written a
  // file, run a command, or built an agent. chat.js says when; what to refresh
  // is this screen's business, not the chat client's.
  async function refreshRail() {
    const body = document.getElementById('rail-body');
    if (!body) return;
    const params = new URLSearchParams();
    if (window.CHAT_THREAD) params.set('chat', window.CHAT_THREAD);
    const scroll = body.scrollTop;
    try {
      const r = await fetch(`/jarvis/rail?${params}`);
      if (!r.ok) return;
      body.innerHTML = await r.text();
      // Rebuilt markup carries none of the state the reader put there.
      body.scrollTop = scroll;
      restoreRail();
    } catch (err) { /* the rail is a view; the conversation is the page */ }
  }

  document.addEventListener('turn-finished', refreshRail);
  document.addEventListener('turn-finished', refreshBench);

  restoreRail();
  stickTerminal();
})();
