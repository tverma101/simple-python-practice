# UX architecture decisions

Decisions taken from a four-agent audit of the existing UI (problem-solving flow,
editor choice, information architecture, accessibility/responsive), recorded here
so the reasoning survives and the numbers can be re-checked.

Each decision names the evidence it rests on. Where the audit contradicted an
assumption — several times — that is called out rather than smoothed over.

---

## 1. CodeMirror 6, not Monaco

**Decision: vendor CodeMirror 6.** The other codebase uses Monaco 0.45 from
jsDelivr. That choice does not survive contact with the constraints.

Measured, both vendored and served by `python -m http.server`, loaded in Chrome:

| | Monaco 0.56 | **CodeMirror 6.0.2** | ACE | Prism |
|---|---:|---:|---:|---:|
| Vendored, Python only | 4.56 MB / 19 files | **1.17 MB / 16 files** | 480 KB | 9.5 KB |
| Gzipped | — | **0.30 MB** | — | ~4 KB |
| Extra CSS to vendor | 350 KB + 1.26 MB theme | **0** | 0 | 0 |
| Works with no build step | deprecated path | **yes** | yes | n/a |
| Web worker required | **yes** | **no** | no | no |
| License | MIT | **MIT (all 15 pkgs)** | BSD-3 | MIT |

Monaco fails two hard constraints outright:

1. **The AMD build is deprecated and unsupported.** Its 0.53.0 changelog says
   *"we don't offer support for it anymore. Please migrate to the ESM build."*
   `docs/integrate-amd.md` now 404s. The old platform depends on the AMD loader.
2. **The ESM build cannot be loaded without a bundler.** It contains ~120
   `import './foo.css'` side-effect imports that no browser resolves natively. The
   unbundled sample was deleted in 0.53.0; every surviving doc assumes
   webpack/vite/parcel. This project has no build step by design.
3. Its README states only `monaco.d.ts` is versioned and *"everything else is
   considered private and might break with any release."*

**Accessibility is the deciding factor, and it is structural.** CodeMirror's
content surface is a `contenteditable` div with `role="textbox"` and
`aria-multiline`, plus official `EditorView.contentAttributes` for a label.
Monaco's is a hidden `ime-text-area` textarea with `tabindex="-1"` and no role;
its `accessibilitySupport: 'on'` mode **disables word wrap**, and its
accessibility help dialog has been broken since 0.40.0 (issues #4908, #4124) —
a WCAG 1.3.1 Level A gap. Monaco's own integrator wiki concedes *"the state of
the art is lacking."*

CodeMirror needs no worker, so it has no `file://` restriction and no silent
failure mode when a language file 404s (Monaco rendered Python entirely
uncoloured, with no error, when its tokenizer failed to load).

**Vendored files** — `static/vendor/cm/`, each a single pre-bundled
`dist/index.js`, loaded through an import map so no bundler is needed:

```
codemirror-state.js  codemirror-view.js  codemirror-language.js
codemirror-commands.js  codemirror-search.js  codemirror-autocomplete.js
codemirror-lang-python.js  codemirror-theme-one-dark.js
lezer-python.js  lezer-common.js  lezer-lr.js  lezer-highlight.js
style-mod.js  w3c-keyname.js  crelt.js  marijn-find-cluster-break.js
```

All MIT; keep each package's `LICENSE` beside its file.

Two operational rules: **pin exact versions** (a missing export such as
`completionTooltip` is a link error that fails *silently* — no console output, no
try/catch, just a dead editor), and **do not hand-roll a `HighlightStyle`**
(verified: it produces zero token spans). Use the built-in
`defaultHighlightStyle` for light and `theme-one-dark` for dark — there is no
official light-theme package.

**Untested:** Firefox and Safari were not available during the evaluation.
CodeMirror uses no workers so the risk is low, but IME and `contenteditable`
behaviour on Safari should be verified before shipping, as Safari is historically
the fussiest here.

---

## 2. The existing layout is unusable on a phone, and the reason is specific

The old two-pane problem view has **zero media queries in 1,792 lines**, and:

```css
.problem-panel { flex: 0 0 45%; min-width: 400px; max-width: 600px; }
body           { overflow: hidden; height: 100vh; }
```

`min-width` is a hard floor, so the editor's width is `max(0, viewport − 400px)`.
At 375px that is **0px**. With `overflow: hidden` on both the container and the
body there is no scrollbar to recover it, so **a student on a phone cannot write,
run, or submit code at all.** The responsive rules that do exist
(`reference_tab.css:546-577`) only change `flex-basis` and `max-width`, never
reset `min-width`, and are scoped to the Reference tab.

The old tab strip also needs ~473px and has no wrap, so below ~888px the Reference
tab is clipped and unclickable — and Submit force-switches to that tab, so
opening Reference and then submitting is a soft lock.

Also inherited: no `100dvh`/`100svh` anywhere, so on mobile the collapsed
toolbar hides the bottom 60–100px of a `100vh` layout that cannot be scrolled to.

**Consequences for the rebuild:** use `100dvh` with a `100vh` fallback, stack the
panes below the breakpoint instead of flexing them, and never put
`overflow: hidden` on `body`.

---

## 3. Three bugs worth naming, because they are the kind that survive review

### 3.1 Hidden test answers were leaked to students

The grader set `expected` on **every** result, including hidden tests
(`code_runner_enhanced.py:678`), and the UI printed it unconditionally
(`view_lesson.html:1569`). With 23,349 hidden tests, failing any one of them
printed the answer. Irreversible once in the DOM.

**Fixed in this repo** at the data layer, not the view layer:
`CaseResult.student_view()` redacts a hidden case's expected value, and
`to_dict(student=True)` is the only path a student-facing route may use. A future
route that forgets to redact is exactly how the original bug happened, so the
default (`student=False`) is the teacher view. Covered by
`TestHiddenTestRedaction`.

### 3.2 Pass and fail rendered identically

`matte_black.css:414-419` overrides `.test-case` with `background: … !important`,
which beats `.test-case.passed` / `.test-case.failed` (specificity 0,2,0) because
`!important` wins regardless of specificity. Both states rendered as identical
grey cards, leaving only a `✓`/`✗` glyph. A complete, responsive, correct
result-card system sat unused in `features/problems.css:361-475` — a file the page
never loads.

### 3.3 The editor was the only script on the page

`view_lesson.html` loads exactly one `<script src>`, the Monaco CDN loader. If it
fails, `require` is `undefined`, line 1013 throws, and **the entire script block
from 935 to 1790 dies** — every tab, every button, autosave, run, submit, toasts.
The page renders as a static image with no error, no fallback editor, no
`noscript`. For a local-first app that is the default state offline.

A second-order failure: if `loader.js` loads but `editor.main` 404s, then
`editor.getValue()` in `submitCode()` throws an uncaught `TypeError` and the
button does nothing at all, silently.

**Consequence for the rebuild:** no remote asset may be on the critical path, and
the editor must degrade to a working `<textarea>` rather than to nothing.

---

## 4. Accessibility: what actually failed, measured

Ratios are computed (WCAG relative luminance, alpha composited over the real
backdrop), not estimated.

- **Contrast is mostly fine** on the dark surfaces — `--text-primary` 19.0:1,
  `--text-secondary` 11.1:1, all of `list.html` and `take.html` body text clear
  AA. Do not "fix" what is not broken.
- **The focus ring fails.** `matte_black.css:58-67` sets
  `box-shadow: 0 0 0 3px rgba(240,241,245,.16)`, which composites to **1.44:1**
  against the page. WCAG 2.2 SC 1.4.11 wants ≥3:1. The quiz variant measures
  **1.31:1**. Worse, the ring only appears where a *border* is also restyled, and
  `.btn { border: 1px solid transparent }` plus
  `.console-input { border: none; outline: none }` mean the console input has
  **no perceptible focus indicator at all**.
- **Every border in the app is sub-1.7:1** against its backdrop (1.16:1 for
  `--border-primary`). Text passes 1.4.3; control boundaries fail 1.4.11.
- **Two invisible headings.** `index.html:171,188` put `#f5f7fb` headings on
  `#fff3cd` and `#d1ecf1` light cards: **1.03:1** and **1.15:1**. They are the
  titles of the page's central selling point.
- **The tab strip is a hard keyboard barrier.** Roving `tabindex` is maintained
  correctly, but there is **no keydown handler**, so the three `tabindex="-1"`
  tabs are unreachable — and both submit paths force-switch into that pane.
- **Zero live regions in the entire learning workspace.** One `aria-live` exists
  across nine templates, on the Reference pane. Test results, verdict toasts,
  console output, autosave failure and the loading overlay are all silent.
- **Quiz `ordering` questions are unanswerable on touch.** HTML5 drag-and-drop
  does not fire on iOS Safari; there is no keyboard handler and no move buttons.
- **Not one of 128 icon-font elements is `aria-hidden`.** Bootstrap Icons render
  via `::before` with a private-use codepoint, so every nav item and button icon
  enters the a11y tree as noise.
- **`prefers-reduced-motion` is honoured in one place**, for five selectors.
  Monaco's `cursorBlinking: 'smooth'`, `cursorSmoothCaretAnimation: true` and
  `mouseWheelZoom: true` all run regardless — the last hijacks scroll-zoom over
  the editor.
- **Disabled state is opacity-only**, measuring 2.53:1 for the Previous/Next
  arrows, which are `<span>`s with no `aria-disabled`.

---

## 5. Information architecture: the data does not support a unit-based course

Four of the stated premises turned out to be wrong, and two change the design.

| Premise | Reality |
|---|---|
| 801 problems have `unit=0` | They are `unit IS NULL`. Zero rows have `unit=0`; the `0` came from a JSON report coercing NULL. |
| `topic_tier` is an independent axis | It is a **perfect alias of `difficulty`** — 1–2 Beginner, 3–4 Intermediate, 5–6 Advanced, zero exceptions in 5,132 rows. |
| `lesson` links problems to lessons | **100% dangling.** 3,454 active values, 0 resolve. `lesson_problems` is the only real link. |
| There is a concept taxonomy | `concepts`, `problem_concepts`, `concept_prerequisites` **exist with the right columns and contain 0 rows.** |

**`unit` is provenance from import batches, not curriculum.** Unit and tier
correlate at Spearman ρ = 0.53 — barely better than noise — and 3,789 problems
(73.8%) sit in at least one inversion. Unit 1 spans all six tiers (177 tier-1
next to 34 tier-6). Unit 2 caps at tier 3 while units 6–8 reach tier 6. A
unit-based navigation would be a lie.

**Lesson membership is the single strongest predictor of shippability:**

| | n | verified |
|---|---:|---:|
| reachable from an active lesson | 1,491 | **90.3%** |
| not reachable | 3,641 | **33.0%** |

That dominates every other signal (tier ranges 16.6–77.0%; unit ranges
0–98.0%). So "what should I do next" is **lesson-gated by construction**: it
never picks an orphan, and therefore never surfaces a problem the student cannot
be graded on.

**The lesson graph is real and acyclic** — 66 resolved prerequisite edges, 0
cycles — but only once you decode that `prerequisites` is a bare `TEXT` column
holding three incompatible formats: JSON int arrays resolving to `lessons.id`,
bare ints resolving to unit-local `lesson_num`, and JSON string arrays resolving
to `slug`. Raw `id` is **not** a valid topological order (30% of edges point
backwards, because ids interleave across units); `(unit_num, lesson_num)` has
**0** backward edges. That pair is the sort key.

**Two conclusions that are uncomfortable and should not be softened:**

1. **Unit 1 is a content ghost and unit 2 is the real beginner path.** Unit 1 is
   pedagogically where a course starts — 12 lessons, clean slug prerequisites,
   titles like *"Python Basics: Type First, Read Second"* — and it has **9 linked
   problems across 12 lessons, 7 of which have none**, at 31% verified. Unit 2 has
   15–18 problems per lesson at **98% verified**. Start Here must launch into
   unit 2, and unit 1 must read "coming soon". That is a content gap; no
   information architecture fixes it.
2. **Tier 1 — the actual beginner tier — is 16.6% verified.** 161 of 193 are
   quarantined. The honest promise to a beginner on day one is
   **156 problems (3.0% of the library)**, not "thousands".

**Also worth knowing:**

- 45% of the library is title-indistinguishable: 3,248 distinct titles for 5,132
  problems, `'check if string contains character'` appearing 37 times. Any browse
  UI that leads with the title is unusable for nearly half the library. Lead with
  topic + difficulty + signature instead.
- 488 titles (9.5%) contain `Problem N` / `Variant N` / `Practice N` and are
  unusable as labels.
- 545 distinct tags, 18% pure metadata (`tier4`, `capstone`, `stabilized`), 8.80
  tags per problem. Singular/plural pairs are **disjoint** (Jaccard 0.000) because
  they are two import batches with two vocabularies. Mechanical curation yields
  110 candidate topics of which 70 are real after removing 28 syntax tokens and
  7 difficulty words — a script can propose merges but cannot decide that `def` is
  not a topic. **This is the one place needing human curation.**
- **Only 85 lessons are active, not 103**; 18 are soft-deleted but still
  referenced by 100 `lesson_problems` rows. Any lesson query must filter
  `is_active=1`.
- **Slugs are globally unique and never recycled** across all 7,371 rows, and
  392 titles collide across the soft-delete boundary. So slug is identity and
  title is not — use `/p/{slug}` and keep topic and difficulty *out* of the path,
  because all three are volatile and slug is not.
- **230 orphans are recoverable for free** by slug prefix. `u3-` slugs sit at
  unit NULL ×218, unit 5 ×6, unit 6 ×23, unit 7 ×59, unit 9 ×12, and *none* at
  `unit=3` — while unit 3 holds 752 soft-deleted rows and no deleted row with a
  `u3-` slug. That is why "unit 3 has 2 problems": the content was replaced by a
  re-import that never set `unit`. A 230-row backfill, not new content.

### Navigation model

Five destinations, built on the two signals that are trustworthy — the
prerequisite DAG and verification status:

1. **Start Here** — one card, the single next thing. Not a browse page.
2. **Path** — the 66-edge DAG, topologically sorted by `(unit_num, lesson_num)`.
3. **Practice** — topic-first faceted browsing. This is where the 70% orphan
   majority lives. 3 clicks to a topic: domain → topic → results.
4. **Lessons** — all 85, with an explicit emptiness badge where a lesson has
   fewer than 5 verified problems.
5. **Reference** — Python syntax/builtins lookup, from the 28 syntax-tag clusters.

Not "Units" (provenance) and not "Difficulty" (an alias of tier).

**The "next" algorithm**, with `MIN_CONTENT = 5` set by the data (9 of 85 active
lessons have zero verified problems, and four have exactly one, so without a
floor a student gets a single problem for a whole lesson):

```
1. lessons = 85 active, resolved-prereq graph, sorted by (unit_num, lesson_num)
2. L = first lesson where all resolved prerequisites are complete
       AND L has >= 5 verified, non-vague-title problems
3. if L has an unstarted verified problem: return it, by display_order
4. if L is complete: return the next lesson's first verified problem
5. fallback (9 active lessons qualify for none of the above):
     highest-scoring verified problem from the Practice pool,
     score = 3/tier + 2*(has canonical topic) + 1*(title not vague)
```

Order by `lesson_problems.display_order` (0 nulls, contiguous for u2l1). Ignore
`coding_problems.problem_order` — 68% NULL.

### 5.1 Re-derived against the database, and two corrections

`src/ducky/curriculum.py` implements the above. Re-running
`python tools/curriculum_report.py --db <source> --verification
reports/verification.json` reproduces these numbers. Most of the audit holds;
three claims needed adjusting, and two steps of the algorithm do not work as
written.

**Confirmed exactly.** 85 active lessons of 103. 21 roots, 12 of them unit 3.
Longest prerequisite chain 15 lessons, ending at the three unit 8 lessons. Zero
cycles. 19 of the 64 fully active edges point backwards under raw `id` (29.7%,
"30%"), zero under `(unit_num, lesson_num)`. Lesson membership still predicts
shippability at 90.3% inside (1,347 of 1,491) against 33.0% outside (1,203 of
3,641). Nine active lessons have zero verified problems; u1l2, u1l7, u1l8 and
u1l9 have exactly one. Tier 1 is 16.6% verified. 488 titles carry a
`Problem N`/`Variant N`/`Practice N` shape. `display_order` has no NULLs and
u2l1 runs 1..18.

**The three formats, corrected.** Over all 103 lessons the column holds 71
tokens: 42 resolve as `lessons.id`, 13 as a slug, and 13 as a unit-local
`(unit_num, lesson_num)`. Those last 13 are *all JSON int arrays*, not bare
ints — unit 2's chain is `[1]`, `[2]`, `[4]`…, where no lesson has id 1 or 2 at
all. No token resolves under both readings, so the totals are unaffected either
way; the loader tries `id` first and then unit-locally, and records which
reading matched on every edge. Five tokens resolve to nothing (`110` on four unit
4 lessons, `114` on one). The active 85-lesson graph keeps 65 of the 66
resolvable edges: the 66th belongs to a soft-deleted lesson, and one edge
(u6l1 → u5l7) names a soft-deleted parent, which is retained and reported but
gates nothing.

**Not confirmed.**

- *"Unit 1 has 9 linked problems across 12 lessons, 7 of which have none, at 31%
  verified."* Measured: 17 linked rows (15 distinct problems — two are linked to
  two lessons each) across 9 of 12 lessons, 3 of which have no link at all, 8 of
  which have no *verified* problem, and 4 of 17 rows verified = 23.5%.
- *"3,248 distinct titles for 5,132 problems."* 3,254.
- *"problem_order — 68% NULL."* 4,870 of 7,371 rows = 66.0% (or 94.9% of the
  5,132 imported). The conclusion — ignore the column — is unaffected.

**Applying the floor changes the count more than expected.** Of the 85 active
lessons, **20** fall below `MIN_CONTENT`, not 9: the extra 11 are u3l3 (18
linked, none verified) and u3l6–u3l12, which have 18 verified problems each and
*every one of them titled `Problem 1` … `Problem 18`*. The vague-title half of
the floor therefore removes seven whole unit 3 lessons from the recommendation
path, not from the library. The greedy walk still reaches 1,220 of the 1,221
distinct usable problems; the one it never reaches is in u1l8, because every unit
1 lesson is below the floor and the walk only moves forward from the first lesson
that clears it. That is the intended consequence of "Start Here launches into
unit 2", stated as a number rather than assumed.

**Two steps of the pseudocode do not work as written.**

- Step 3 says "unstarted". Taken literally, a student who starts problem 2 and
  fails it is never offered problem 2 again — not while the rest are outstanding,
  and not afterwards, because by then the lesson is complete. The implementation
  returns the first problem that is not *complete*, so a started problem is the
  first candidate rather than a skipped one.
- Step 4 says "the next lesson's first verified problem". Taken literally it
  re-serves finished work: completing u2l1 hands back u2l2's first problem, and
  the next call hands back the same one, forever. The implementation walks
  forward past finished lessons to the first problem that is not complete. It
  does *not* re-apply `MIN_CONTENT` inside that walk, because u1l2/u1l7/u1l8/u1l9
  are each a prerequisite of the next unit 1 lesson and skipping them would leave
  verified problems in lessons that can never become ready.

---

## 6. What this means for the build

Non-negotiables carried into implementation:

- No remote asset on any critical path; the editor degrades to a `<textarea>`.
- `100dvh` with `100vh` fallback; stacked panes below the breakpoint; never
  `overflow: hidden` on `body`.
- Every interactive state designed: focus, hover, active, loading, empty, error,
  disabled. Not only the happy path.
- Keyboard-complete: the tab strip needs arrow/Home/End, results need a live
  region, focus must move to results on render and must not be left inside a
  `display: none` subtree.
- Honest save state, with a `beforeunload` guard and an offline buffer. The old
  autosave reported failure only via `console.error` and had no retry, so a
  dropped connection meant silent data loss.
- No 3-hour silent submits. Bound every grading call, stream or poll per test,
  lock the button, offer cancel. The old path ran one subprocess per test at 5s
  each, serially — up to ~180s for 36 tests, with double-click firing two runs
  and double-counting XP.
- Icons `aria-hidden`; no meaning carried by emoji or colour alone.
- A focus indicator that measures ≥3:1, everywhere, including inputs with
  `border: none`.
- Respect `prefers-reduced-motion`, and do not hijack scroll-zoom.
- Minimum 44×44px targets, applied as a rule rather than a hand-listed set of
  class names — the old `min-height: 44px` list missed Submit itself.
- Exclude quarantined problems from student navigation entirely. Routing a
  student to a problem whose tests are broken means marking them wrong for being
  right, which is worse than the problem not existing. Surface those in a
  teacher-facing repair queue only.
