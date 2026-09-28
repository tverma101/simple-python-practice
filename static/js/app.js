/* Shared app behaviour: sign-in, and an honest save indicator.
 *
 * The previous UI had no way to obtain a session. problem.js posted {slug, code}
 * with no credential of any kind, the index form had no action and no handler,
 * and nothing called POST /api/sign-in -- so every Run and Submit returned 401
 * and the primary feature simply did not work. The tests missed it because they
 * all authenticated with `access_code` inside the JSON body, a payload the
 * browser never sends.
 *
 * Sign-in is a form post that exchanges the code for an HttpOnly session cookie,
 * so the code is typed once rather than on every keystroke-triggered autosave.
 */

const CODE_KEY = 'ducky-access-code';

function storedCode() {
  try { return sessionStorage.getItem(CODE_KEY) || ''; } catch { return ''; }
}

function storeCode(code) {
  try { sessionStorage.setItem(CODE_KEY, code); } catch { /* private mode */ }
}

/* Send the access code on every mutating request.
 *
 * The header rather than a body field, because the body shape belongs to each
 * endpoint and duplicating the credential into it is how it ends up logged.
 */
export function authHeaders(extra = {}) {
  const code = storedCode();
  return code ? { ...extra, 'X-Ducky-Access-Code': code } : { ...extra };
}

export async function signIn(code) {
  const response = await fetch('/api/sign-in', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ access_code: code }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || 'That access code was not accepted.');
  }
  storeCode(code);
  return payload;
}

export function hasCode() {
  return Boolean(storedCode());
}

/* Wire up a form with id="signin" and a status element id="signin-status".
 *
 * The status element carries role="status" in the markup, so a screen reader
 * announces the outcome without this needing an aria-live region.
 */
export function initSignIn() {
  const form = document.getElementById('signin');
  const status = document.getElementById('signin-status');
  const field = document.getElementById('access_code');
  if (!form || !status) return;

  if (field) field.value = storedCode();

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = (field && field.value ? field.value : '').trim();
    if (!code) {
      status.textContent = 'Enter your access code.';
      return;
    }
    status.textContent = 'Checking…';
    try {
      const who = await signIn(code);
      status.textContent = `Signed in as ${who.name}. You can run and submit now.`;
      form.hidden = true;
    } catch (err) {
      status.textContent = err.message;
    }
  });

  if (hasCode()) {
    status.textContent = 'Access code remembered for this tab.';
  }
}
