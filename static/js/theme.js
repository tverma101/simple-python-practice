/* Theme toggle. Persists an explicit choice; falls back to the OS setting.
 *
 * The old UI was dark-only with the editor hardcoded to `vs-dark`, so a
 * light-theme user had no way to read the page comfortably. Both themes are
 * defined in tokens.css, and the editor follows this choice.
 */

const KEY = 'ducky-theme';
const root = document.documentElement;
const button = document.getElementById('theme-toggle');

function systemTheme() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function current() {
  return root.dataset.theme || systemTheme();
}

function apply(theme) {
  root.dataset.theme = theme;
  if (button) {
    button.setAttribute('aria-pressed', String(theme === 'dark'));
  }
  // Let the editor module re-theme if it has mounted.
  window.dispatchEvent(new CustomEvent('ducky:theme', { detail: { theme } }));
}

function stored() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

apply(stored() || systemTheme());

if (button) {
  button.addEventListener('click', () => {
    const next = current() === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(KEY, next); } catch { /* private mode */ }
    apply(next);
  });
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (event) => {
  if (!stored()) apply(event.matches ? 'dark' : 'light');
});
