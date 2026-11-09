// ui_util.js — small, dependency-free UI helpers extracted from index.html.
//
// First step of splitting the monolithic inline SPA into ordered <script src>
// modules. These are pure/global helpers with no app-state coupling, loaded
// BEFORE the main inline script so `X`, `fmtN`, `fmtBytes`, `toast` are already
// global when the app uses them. Keep them as classic global function
// declarations (NOT ES modules) — the inline onclick handlers rely on globals.

// HTML-escape a value for safe interpolation into innerHTML.
function X(s) {
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Compact integer formatting: 1.2k / 3.4M.
function fmtN(n) {
  if (n>=1e6) return (n/1e6).toFixed(1)+'M';
  if (n>=1000) return (n/1000).toFixed(1)+'k';
  return String(n||0);
}

// Human-readable byte sizes.
function fmtBytes(b) {
  if (b>1048576) return (b/1048576).toFixed(1)+' MB';
  if (b>1024)    return (b/1024).toFixed(0)+' KB';
  return b+' B';
}

// Loading placeholder: shimmering lines in the shape of the list that is coming,
// announced once to a screen reader.
function skel(lines = 3) {
  const w = ['w80', '', 'w60', 'w40'];
  let h = '<div class="skel-wrap" role="status" aria-busy="true"><span class="sr-only">Loading</span>';
  for (let i = 0; i < lines; i++) h += `<div class="skel skel-line ${w[i % w.length]}"></div>`;
  return h + '</div>';
}
function skelRow(cols) {
  return `<tr><td colspan="${cols}">${skel(3)}</td></tr>`;
}

// Toast notification. type ∈ info|success|error|warn.
// opts.duration (ms), opts.action {label, fn} for an inline button (e.g. Undo).
// An error stays until it is closed: a message that vanishes before it is read is
// no message at all.
function toast(msg, type='info', opts={}) {
  const icons = {info:'ℹ️', success:'✓', error:'✗', warn:'⚠'};
  const cls   = {info:'toast-info', success:'toast-success', error:'toast-error', warn:'toast-warn'};
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast-item ${cls[type]||cls.info}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const sticky = type === 'error' && !opts.duration;
  const duration = opts.duration || 3500;
  el.innerHTML = `<span class="toast-icon">${icons[type]||icons.info}</span><span style="flex:1">${X(msg)}</span>`;
  if (opts.action) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-ghost btn-sm toast-action';
    btn.textContent = opts.action.label || 'Undo';
    btn.onclick = () => { try { opts.action.fn(); } finally { el.remove(); } };
    el.appendChild(btn);
  }
  if (sticky) {
    const close = document.createElement('button');
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '×';
    close.onclick = () => el.remove();
    el.appendChild(close);
  }
  container.appendChild(el);
  if (sticky) return el;
  const tm = setTimeout(() => {
    el.classList.add('toast-exit');
    el.addEventListener('animationend', () => el.remove());
  }, duration);
  el._tm = tm;
  return el;
}
