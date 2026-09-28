/* Editor bootstrap.
 *
 * Loads the vendored CodeMirror through an inline import map and falls back to a
 * plain <textarea> if anything at all goes wrong.
 *
 * The fallback is not defensive decoration. The previous codebase's problem page
 * had exactly one <script src>: a CDN URL for Monaco's AMD loader. When it failed
 * to load, `require` was undefined, the next line threw, and that killed the
 * entire script block -- every tab, every button, autosave, run and submit. The
 * page rendered as a static image with no error anywhere.
 *
 * CodeMirror fails differently and just as silently: a bare specifier missing
 * from the import map produces a dead editor with no console output. So the whole
 * module graph loads inside one try/catch and the textarea is the floor. A
 * beginner platform must not be unusable because a script did not load.
 */

const HOST = document.getElementById('editor-host');
const FALLBACK = document.getElementById('editor-fallback');

let view = null;
let changeHandlers = [];
let getImpl = () => (FALLBACK ? FALLBACK.value : '');
let setImpl = () => {};

export function getCode() { return getImpl(); }

export function setCode(text) { setImpl(text); }

export function isFallback() { return view === null; }

function emit(text) {
  changeHandlers.forEach((fn) => {
    try { fn(text); } catch (err) { console.error(err); }
  });
}

/* Register a change listener.
 *
 * Two things here are load-bearing, and both were wrong before.
 *
 * `EditorView.updateListener` is a **static** Facet reached through the
 * constructor, not an instance member, so `view.updateListener` is undefined and
 * `.of()` on it threw a TypeError at page boot. That call sat outside
 * initEditor's try/catch, so the fallback never engaged either -- autosave never
 * ran at all, silently, behind an empty save indicator and an un-armed
 * beforeunload guard.
 *
 * A Facet cannot be introduced by a dispatch after the state exists, so the
 * listener is installed in the extension list initEditor passes. This function
 * only appends to the handler list.
 */
export function onChange(handler) {
  changeHandlers.push(handler);
  if (!view && FALLBACK) {
    FALLBACK.addEventListener('input', () => emit(FALLBACK.value));
  }
}

function useFallback(reason) {
  view = null;
  getImpl = () => (FALLBACK ? FALLBACK.value : '');
  setImpl = (text) => { if (FALLBACK) FALLBACK.value = text; };
  if (FALLBACK) FALLBACK.hidden = false;
  if (HOST) HOST.hidden = true;
  const status = document.getElementById('grade-status');
  if (status) {
    status.textContent = 'Plain editor: advanced editing is unavailable, but you can still work.';
    status.dataset.state = 'error';
  }
  console.warn('editor fallback engaged:', reason);
}

export async function initEditor(initialText) {
  if (!HOST) { useFallback('no host element'); return; }
  try {
    const viewMod = await import('@codemirror/view');
    const stateMod = await import('@codemirror/state');
    const commands = await import('@codemirror/commands');
    const search = await import('@codemirror/search');
    const language = await import('@codemirror/language');
    // closeBrackets is exported by @codemirror/autocomplete, not
    // @codemirror/language. Importing it from the wrong package throws at
    // destructuring time.
    const autocomplete = await import('@codemirror/autocomplete');
    const pythonMod = await import('@codemirror/lang-python');
    const oneDarkMod = await import('@codemirror/theme-one-dark');

    const {
      EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection,
      rectangularSelection, crosshairCursor, highlightSpecialChars,
    } = viewMod;
    const { EditorState, Compartment } = stateMod;
    const { history, historyKeymap, defaultKeymap, indentWithTab } = commands;
    const { searchKeymap, highlightSelectionMatches } = search;
    const {
      defaultHighlightStyle, syntaxHighlighting, bracketMatching,
      indentOnInput, foldGutter, foldKeymap,
    } = language;
    const { autocompletion, closeBrackets, closeBracketsKeymap } = autocomplete;
    const { python } = pythonMod;
    const { oneDark } = oneDarkMod;

    // Must be part of the initial extension set: reconfiguring a Compartment
    // that was never added is a silent no-op.
    const theme = new Compartment();
    const dark = (document.documentElement.dataset.theme || 'light') === 'dark';

    view = new EditorView({
      parent: HOST,
      state: EditorState.create({
        doc: initialText || '',
        extensions: [
          lineNumbers(), highlightActiveLine(), drawSelection(),
          rectangularSelection(), crosshairCursor(), highlightSpecialChars(),
          history(), foldGutter(), indentOnInput(), bracketMatching(),
          closeBrackets(), autocompletion(), highlightSelectionMatches(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          python(),
          keymap.of([
            ...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap,
            ...searchKeymap, ...foldKeymap, indentWithTab,
          ]),
          // Smooth caret and smooth scrolling are documented vestibular
          // triggers and ran regardless of preference in the old build.
          EditorView.theme({
            '&': { height: '100%' },
            '.cm-scroller': { overflow: 'auto' },
          }),
          theme.of(dark ? oneDark : []),
          // An accessible name for the editing surface. The old host div had no
          // role, no label, and `outline: none`, so it announced as nothing and
          // showed no focus ring at all.
          EditorView.contentAttributes.of({ 'aria-label': 'Python code editor' }),
          // Installed here, not added later: a Facet cannot be introduced by a
          // dispatch once the state exists.
          EditorView.updateListener.of((update) => {
            if (update.docChanged) emit(update.state.doc.toString());
          }),
        ],
      }),
    });

    getImpl = () => view.state.doc.toString();
    setImpl = (text) => {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    };

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      const style = document.createElement('style');
      style.textContent = `.cm-editor .cm-cursor, .cm-editor .cm-scroller,
        .cm-editor .cm-tooltip { animation: none !important;
        transition: none !important; scroll-behavior: auto !important; }`;
      document.head.appendChild(style);
    }

    window.addEventListener('ducky:theme', (event) => {
      view.dispatch({
        effects: theme.reconfigure(event.detail.theme === 'dark' ? oneDark : []),
      });
    });

    if (FALLBACK) FALLBACK.hidden = true;
  } catch (err) {
    useFallback(err);
  }
}
