/* Problem page controller.
 *
 * Each numbered fix below corresponds to a defect the audit measured in the
 * previous implementation.
 */

import { getCode, setCode, initEditor, onChange, isFallback } from './editor.js';
import { authHeaders, hasCode, initSignIn } from './app.js';

const SLUG = document.body.dataset.slug;
const STARTER = document.body.dataset.starter || '';

/* ---- 1. Tabs: keyboard-complete, and results are announced + focused ----
 *
 * The old tab strip had correct ARIA and roving tabindex but *no keydown
 * handler*, so the three tabindex="-1" tabs were unreachable from the keyboard
 * -- and both of its submit paths force-switched results into that unreachable
 * pane. A student could open Reference, submit, and have focus left inside a
 * display:none subtree with nowhere to type.
 */
const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
const panels = tabs.map((t) => document.getElementById(t.getAttribute('aria-controls')));

function selectTab(index, { focus = true } = {}) {
  tabs.forEach((tab, i) => {
    const active = i === index;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    panels[i].hidden = !active;
  });
  if (focus) tabs[index].focus();
}

tabs.forEach((tab, i) => {
  tab.addEventListener('click', () => selectTab(i, { focus: false }));
  tab.addEventListener('keydown', (event) => {
    const last = tabs.length - 1;
    let next = null;
    switch (event.key) {
      case 'ArrowRight': next = i === last ? 0 : i + 1; break;
      case 'ArrowLeft': next = i === 0 ? last : i - 1; break;
      case 'Home': next = 0; break;
      case 'End': next = last; break;
      default: return;
    }
    event.preventDefault();
    selectTab(next);
  });
});

function showResults() {
  const index = tabs.findIndex((t) => t.getAttribute('aria-controls') === 'panel-results');
  if (index >= 0) selectTab(index, { focus: false });
}

/* ---- 2. Save state: honest, visible, and it blocks navigation on failure ----
 *
 * The old autosave reported failure only through console.error, with no retry,
 * no indicator, and no beforeunload guard. A dropped connection meant silent
 * data loss while the student believed their work was safe.
 */
const saveState = document.getElementById('save-state');
let saveTimer = null;
let savePending = false;
let saveFailed = false;

function setSaveState(state, text) {
  if (!saveState) return;
  saveState.dataset.state = state;
  saveState.textContent = text;
}

async function saveDraft() {
  if (savePending) return;
  savePending = true;
  setSaveState('saving', 'Saving…');
  try {
    const response = await fetch('/api/draft', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'same-origin',
      body: JSON.stringify({ slug: SLUG, code: getCode() }),
    });
    if (response.status === 401) {
      saveFailed = true;
      setSaveState('error', 'Not signed in — your work is only in this tab');
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    // Branch on the field, not the status. The server answers 200 with
    // saved:false when no draft store is configured, and checking only the HTTP
    // status displayed "Saved" for a save that never happened -- with a
    // beforeunload guard behind it that therefore never fired.
    const body = await response.json().catch(() => ({}));
    if (body.saved === false) {
      saveFailed = true;
      setSaveState('error', 'Not saved — copy your code before leaving');
      return;
    }
    saveFailed = false;
    setSaveState('saved', 'Saved');
  } catch (err) {
    saveFailed = true;
    setSaveState('error', 'Not saved — copy your code before leaving');
  } finally {
    savePending = false;
  }
}

function scheduleSave() {
  clearTimeout(saveTimer);
  setSaveState('saving', 'Unsaved changes');
  saveTimer = setTimeout(saveDraft, 1200);
}

window.addEventListener('beforeunload', (event) => {
  if (saveFailed || savePending) {
    event.preventDefault();
    event.returnValue = '';
  }
});

/* ---- 3. Grading: bounded, abortable, and the buttons lock ----
 *
 * The old submit spawned one subprocess per test at a 5s timeout each, serially
 * -- up to ~180s for 36 tests -- with no client timeout, no progress, no cancel,
 * and no button lock, so a double-click fired two runs and double-counted XP.
 */
const runButton = document.getElementById('run');
const submitButton = document.getElementById('submit');
const resetButton = document.getElementById('reset');
const cancelButton = document.getElementById('cancel');
const gradeStatus = document.getElementById('grade-status');
const resultsRegion = document.getElementById('results-region');
const resultsCount = document.getElementById('results-count');

let inFlight = null;

function setBusy(busy) {
  [runButton, submitButton, resetButton].forEach((b) => {
    if (!b) return;
    b.disabled = busy;
    b.setAttribute('aria-disabled', String(busy));
  });
  if (cancelButton) cancelButton.hidden = !busy;
}

async function grade(mode) {
  if (inFlight) return;
  const controller = new AbortController();
  inFlight = controller;
  setBusy(true);
  gradeStatus.dataset.state = 'saving';
  gradeStatus.textContent = mode === 'run' ? 'Running…' : 'Submitting…';
  showResults();

  const started = performance.now();
  let payload;
  try {
    const response = await fetch(mode === 'run' ? '/api/run' : '/api/grade', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'same-origin',
      signal: controller.signal,
      body: JSON.stringify({ slug: SLUG, code: getCode() }),
    });
    const elapsed = Math.round(performance.now() - started);
    if (response.status === 401) {
      // Show the sign-in field here rather than telling the student to go to the
      // home page: they are mid-problem, possibly on a phone, and their code is
      // on screen. Navigating away to re-enter a code is how work gets lost.
      revealSignIn();
      renderNotice('error', hasCode()
        ? 'That access code was not accepted. Enter a current one here.'
        : 'Enter your access code to run code.');
      return;
    }
    if (response.status === 429) {
      renderNotice('warn', 'Too many runs in a row. Wait a moment and try again.');
      return;
    }
    if (!response.ok) {
      // A session that expired returns a redirect to an HTML login page, which
      // used to surface as "Unexpected token '<', ... is not valid JSON". Report
      // the status instead of the parse error.
      renderNotice('error', `The server returned ${response.status}. Try signing in again.`);
      return;
    }
    payload = await response.json();
    if (mode === 'run') {
      // Run only checks the samples. Reporting "3 of 8 passed" would imply the
      // hidden cases ran too, so name the set that actually ran. Read after the
      // parse -- this used to sit above it and read an undefined `payload`.
      const samples = (payload && payload.cases_total) || 0;
      if (samples) {
        renderNotice('info',
          `Checked against the ${samples} sample test${samples === 1 ? '' : 's'} only. `
          + 'Submit when you want every test to run.');
      }
    }
    renderResults(payload, elapsed);
  } catch (err) {
    if (err.name === 'AbortError') {
      gradeStatus.textContent = 'Stopped';
    } else {
      renderNotice('error', 'Could not reach the server. Is it still running?');
    }
  } finally {
    inFlight = null;
    setBusy(false);
  }
}

if (cancelButton) {
  cancelButton.addEventListener('click', () => { if (inFlight) inFlight.abort(); });
}
if (runButton) runButton.addEventListener('click', () => grade('run'));
if (submitButton) submitButton.addEventListener('click', () => grade('submit'));

/* ---- 4. Reset: reversible, and it does not destroy the draft ----
 *
 * The old Reset asked a native confirm(), then wrote the starter over the
 * editor, which fired the change handler and autosaved the destruction within
 * two seconds. There was no undo.
 */
if (resetButton) {
  resetButton.addEventListener('click', () => {
    const previous = getCode();
    setCode(STARTER);
    const undo = document.createElement('button');
    undo.type = 'button';
    undo.className = 'btn btn--ghost';
    undo.textContent = 'Undo reset';
    undo.addEventListener('click', () => { setCode(previous); undo.remove(); });
    resetButton.after(undo);
    setTimeout(() => undo.remove(), 8000);
  });
}

/* ---- 5. Results: announced, focused, and never colour-only ---- */

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function renderResults(data, elapsed) {
  const failures = data.failures || [];
  const passed = Boolean(data.passed);

  gradeStatus.dataset.state = passed ? 'saved' : 'error';
  gradeStatus.textContent = `${data.cases_passed}/${data.cases_total} passed in ${elapsed}ms`;

  if (resultsCount) resultsCount.textContent = String(failures.length);

  const heading = passed
    ? `All ${data.cases_total} tests passed.`
    : `${data.cases_passed} of ${data.cases_total} passed.`;

  let html = `<div class="verdict verdict--${passed ? 'pass' : 'fail'}">
    <span class="verdict__icon" aria-hidden="true">${passed ? '✓' : '✗'}</span>
    <span class="verdict__text">${escapeHtml(heading)}
      ${data.detail ? `<span class="verdict__detail">${escapeHtml(data.detail)}</span>` : ''}
    </span>
  </div>`;

  if (data.cases_hidden) {
    html += `<p class="notice" style="margin-top:var(--space-3)">
      ${data.cases_hidden} hidden test${data.cases_hidden === 1 ? '' : 's'} also ran.
      Their expected output is not shown, so a failure there tells you the shape
      of the problem is wrong without giving away the answer.
    </p>`;
  }

  if (failures.length) {
    html += '<div class="results" style="margin-top:var(--space-4)">';
    for (const failure of failures) {
      const label = failure.is_hidden ? 'Hidden test' : `Test ${failure.index + 1}`;
      html += `<details class="result result--fail">
        <summary class="result__head">
          <span class="result__verdict">✗ ${escapeHtml(label)}</span>
          <span style="color:var(--text-secondary);font-weight:400">${escapeHtml(failure.reason || '')}</span>
        </summary>
        <div class="result__body">
          <div class="kv"><span class="kv__k">Expected</span><code class="kv__v output">${escapeHtml(failure.expected_repr)}</code></div>
          <div class="kv"><span class="kv__k">Got</span><code class="kv__v output">${escapeHtml(failure.actual_repr)}</code></div>
        </div>
      </details>`;
    }
    html += '</div>';
  }

  resultsRegion.innerHTML = html;
  // Move focus so a keyboard or screen-reader user is taken to the outcome
  // rather than left on a Submit button with the verdict 600px away.
  resultsRegion.setAttribute('data-programmatic-focus', '');
  resultsRegion.focus({ preventScroll: true });
  resultsRegion.scrollIntoView({ block: 'nearest' });
}

function renderNotice(kind, message) {
  gradeStatus.dataset.state = kind === 'error' ? 'error' : 'saving';
  gradeStatus.textContent = message;
  resultsRegion.innerHTML = `<div class="notice notice--${kind}">${escapeHtml(message)}</div>`;
}

/* ---- Restore a saved draft ----
 *
 * Drafts were write-only: the POST existed and worked, but nothing ever asked
 * for one, so a returning student got the starter code and silently lost
 * whatever they had written. Only applies when the editor was never touched,
 * so an explicit Reset is not undone by a reload.
 */
async function restoreDraft() {
  try {
    const response = await fetch(`/api/draft/${encodeURIComponent(SLUG)}`, {
      credentials: 'same-origin',
      headers: authHeaders(),
    });
    if (!response.ok) return;
    const payload = await response.json().catch(() => ({}));
    const saved = (payload && payload.code) || '';
    if (saved && saved !== getCode()) {
      setCode(saved);
      setSaveState('saved', 'Restored your last save');
    }
  } catch {
    // No draft, or the server is gone. The starter code is a fine place to be.
  }
}

/* ---- boot ---- */

/* Reveal the inline access-code field and focus it. */
function revealSignIn() {
  const form = document.getElementById('signin');
  if (!form) return;
  form.hidden = false;
  const field = document.getElementById('access_code');
  if (field) field.focus();
}

(async function start() {
  // Wire the inline form first, so a student who lands here without a code can
  // sign in without reloading.
  initSignIn();
  await initEditor(STARTER);
  onChange(scheduleSave);
  if (isFallback()) {
    // Autosave does work in the fallback: onChange registers on the textarea.
    setSaveState('idle', 'Plain editor');
  }
  await restoreDraft();
})();
