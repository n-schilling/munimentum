# Design guide

How the interface in `page.html` is built, and where a new element goes so
that the page still looks like one piece afterwards. Read this before
touching markup, CSS or the strings in `lang/`. The rules are short on
purpose; the reasoning behind each is one sentence.

## 1. The one idea: two doors

The app does two things, and the header says exactly that:

- **Build archive** (`nav.export`) — everything about getting data in:
  the state in one line, the sources, the run window.
- **Search archive** (`nav.search`) — everything about getting data out:
  search, filters, hits, calendar, address book, files.

Two smaller rooms sit at the right of the header: **Overview**
(`nav.analytics`, what the archive holds) and **Settings**. Nothing else
becomes a top-level tab; `tests/test_app.py::test_die_reiterzeile_bleibt_kurz`
counts four and fails on a fifth.

**The two doors stay bare.** Build archive and Search archive carry only
what the action at hand needs: the state in one line, the controls, and
while something runs, the run window or its pill. No counts, no history,
no schedule line, no notice, no prose. Whatever explains goes into an `(i)`; whatever
happened goes into Overview; whatever can be configured goes into
Settings — including the notice about a newer release, which sits under
*Settings › App* with a dot on that entry. If a new element would add a
line to one of the two doors, it belongs somewhere else.

Every screen has **one primary action**, blue, and it is the thing a
first-time user should press. Build archive: *Update archive now*. Search
archive: *Search*. Overview: *Check now*. Settings: *Save settings* (only
shown when something changed).

State is shown **where it is fixed**, not in a status bar:

| State | Where |
|---|---|
| Access (token / sign-in) | the state frame in the header (`#zustaende`), click opens the wizard |
| AI (Ollama) | dot next to *AI (Ollama)* in the settings sub-navigation |
| Claude (MCP) | dot next to *Claude (MCP)* in the settings sub-navigation |
| How current the archive is | the one line of the status card on *Build archive* |
| Counts, runs, completeness | Overview |
| Per source: folders chosen, last run | the `(i)` on that source's card, and the summary line of its settings block |
| A running job, and its log | the run window (`#lauf-overlay`), open until the run is done and closed by hand; minimised, the run state in the header frame |
| A newer release | banner under *Settings › App*, dot on the *App* entry |

The header has three kinds of things and one shape for each:

- **Navigation**: the four tabs. The two doors as underlined tabs, the two
  side rooms as small boxed tabs (`.nav-neben`). Nothing else becomes a
  tab.
- **State**: one frame (`#zustaende`, 8 px, one line) holding every state
  that must be seen from every page, each a `.zustand` — a dot or an icon
  plus a word — separated by hairlines, and each a click to the place
  where it is fixed. Today: a minimised run (`#lauf-pille`, only while one
  exists) and the access (`#pill-token`). A new state that truly belongs
  up here goes into this frame as another `.zustand`, never beside it.
- **Action**: one icon button (`.ikonknopf`, 8 px) — *Quit*. A second one
  is allowed only for an action that must be reachable from every page;
  it sits next to it, same size, name in the tooltip.

No pills, no third radius: everything up here is text, or 8 px.

## 2. Anatomy of each area

**Build archive** (`#tab-export`), top to bottom:

1. Status card (`#stand-karte`): one headline with the archive's date and
   the primary button — nothing else. On an empty archive it turns into
   the three-step first start. A running job does not change this card
   beyond the disabled button: it opens the **run window**.
2. *What goes into the archive*: a grid of **source cards** (`.quelle`),
   one per source: name, an `(i)` whose tooltip carries its state, the
   gear (`.zahnrad`) leading to that source in the settings, and chips for
   its categories. Nothing below the chips except an actionable warning
   (`.chk-note`).

**Search archive** (`#tab-suche`):

1. Search row: one field with the mode switch (`.modi`) inside it, one
   button.
2. Filter row (`#filter`): always visible, every filter a pill; a set
   filter is highlighted, *Clear filters* appears only when one is set.
3. View tabs (`.sicht`): Hits, Calendar, Address book, Files.
4. Hits: list on the left, the selected hit on the right with its
   actions (open original, whole conversation, find similar, only this
   person). The AI answer sits above the list and cites into it.

**The run window** (`#lauf-overlay`, the wizards' frame, wider):
everything about a running export — headline with step and start time,
progress bar, step list, the log (`#protokoll`) with *Copy* and *Report a
problem*, *Cancel*. It opens when a run starts here or when the page is
opened while a run is on (by hand or by the schedule); a scheduled run
that starts while someone works on the page appears as the pill first. It
stays until the run is done, then shows the result (headline with count
and duration, the steps with their numbers) until *Close*. *Minimise*
hides it and shows the run state in the header frame; a click there reopens it. It
sits below the wizards' overlay, so an expired token can still ask on
top.

**Overview** (`#tab-analytics`): tiles, charts, completeness, all runs.
Read-only apart from *Refresh* and *Check now*.

**Settings** (`#tab-einstellungen`): sub-navigation on the left
(`.snav`), one card per topic on the right, in the order of the
navigation: Sources, Schedule, AI (Ollama), Claude (MCP), Storage, App,
Expert mode. Each source is a collapsible block (`details.quelle-einst`)
with the essential fields open and everything else under *Advanced*. The
save bar is sticky at the bottom and appears only with unsaved changes.

## 3. Tokens

Colours are CSS variables on `:root`, with a dark set under
`prefers-color-scheme: dark`. Use the variables, never a literal colour in
markup or JS.

| Variable | Light | Use |
|---|---|---|
| `--bg` | `#f6f7f9` | page ground |
| `--card` | `#fff` | cards, header |
| `--ink` | `#1b1f24` | text |
| `--muted` | `#5b6570` | secondary text, icons |
| `--line` | `#dfe3e8` | borders, dividers |
| `--accent` | `#2f6fed` | primary button, active tab, links, chips on |
| `--accent-weich` | `#eaf0fe` | background of a selected/on state |
| `--ok` / `--warn` / `--err` | `#1a7f4b` / `#a2650a` / `#b3261e` | dots, tags, messages |
| `--code` | `#f1f3f6` | code, tags, bars |
| `--konsole` | `#0d1013` | the log |

Type: `15px/1.5 system-ui`. Headings are small and heavy (`h2` 15 px 650,
`h3` 14 px 650); nothing on the page is larger than 18 px except the KPI
values (26 px). `.small` is 13 px, `.muted` uses `--muted`. Numbers that
are compared sit in `.num` (tabular figures).

Spacing: 24 px page gutter, 16 px between cards, 18 px card padding,
12 px between cards in a grid, 6–8 px between chips. Radii: 12 px cards,
10 px inner boxes, 8 px buttons and fields, 999 px chips and pills.

Icons are inline SVG, 16 px in headings and rows, 12 px inside chips and
links, `stroke-width` 2 (2.5 for chevrons, 3 for the chip check). No
emoji, no icon fonts, no images. The set lives in the `IKON` object in the
script; add a new icon there once and reuse it. Static markup names its
icon with `data-ikon="mail"` (plus `data-ikon-klasse` for a size), which
`setzeIkons()` fills in at load; generated HTML calls `ikon('mail')`.

## 4. Components

Use the existing class; do not invent a sibling that looks almost the same.

| Component | Class | Notes |
|---|---|---|
| Card | `.card` | white, 1 px line, 12 px radius, 18 px padding |
| Section head | `.abschnitt-kopf` | `h2` + muted sub line + optional action at the right |
| Source card | `.quelle` | `.qkopf` with `h3` (icon, name, `(i)` with the state) and `.zahnrad`, then `.chips` |
| Chip (choice) | `label.chip` with a hidden checkbox | on = soft accent background, check icon appears |
| Chip (view/segment) | `.chip` or `.modi button` | exactly one `.on` in a group |
| Primary button | `button.act` | one per screen; `.gross` for the archive button |
| Secondary button | `button.ghost` / `a.ghost` | bordered, no fill |
| Small action | `button.mini` / `a.mini` | inside cards and rows |
| Inline link | `a` | for navigation between places, never for a mutation |
| Setting row | `.feldzeile` | label `.bez` with an `(i)` at the left, control at the right |
| Wide row | `.feldzeile.breit` | textarea below its title row |
| Switch | `.kipp` / checkbox inside `.feldzeile` | every boolean is this switch |
| URL table | `.urltab .zeile` | one row per URL: field, cadence, *Sync now* |
| Action row | `.aktionen` | status text left, small buttons right |
| Tag | `.tag` | source, deleted, origin; `.tag.weg` for deleted |
| Status dot | `.dot.ok/.warn/.err` | always next to a word, never alone |
| State line | `.stand` | dot + word, next to a field |
| Info | `.info` | 17 px circle with an `i`; text in `data-i18n-title` |
| Banner | `.banner`, `.banner.warn`, `.banner.err` | one sentence, optional link |
| Modal | `.modal` via `modalKopf(title, kind, info)` + `modalFuss` | cross top right, primary action bottom left; what the window is for and where data stays goes into the `(i)` at the title, not into paragraphs |
| Wizard state | `.banner` with `.dot` + one sentence | "Connected as … — valid for another …"; nothing about what happens next |
| Console | `#log`, `.lauflog` | dark, monospace, 12 px, lines coloured by level; in the run window only the current run's lines (from the job's `log_seq` on) |
| Step list | `.schritte .schritt` | icon, name, detail, duration |
| Run window | `#lauf-overlay` / `.lauf-fenster` | modal frame; `#fortschritt` while running, `#lauf-ergebnis` afterwards, `#protokoll`, footer with *Close* / *Cancel* |
| Header state | `.zustaende .zustand` | dot or icon + word inside the one frame; `.lauf-pille` adds the `.mini-balken` |
| Icon button | `.ikonknopf` | 32 px square, 8 px radius, name in the tooltip; the header's *Quit* |
| KPI tile | `.kpi` | value, title, hint; `.klickbar` when it leads somewhere |
| Hit row | `.hit` | icon, title, date, who, preview; `.on` when selected |
| Detail | `#detail` | `#detail-inhalt` with tag, title, meta, `.daktionen`, content, path; `#detail-verlauf` for the thread |
| Settings navigation | `.snav .snav-punkt` | one per topic card, `data-ziel` names the card; a `.stand` or `.dot` at the right |
| Source block | `details.quelle-einst` | summary with name and state line `.zf`, `.qinhalt`, `details.erweitert` |
| Filter pill | `.filter` or the control itself in `.filterzeile` | `.on` while a value is set |
| View tab | `.sicht` | exactly one `.on` under `#sichten` |

## 5. Where a new element goes

**A new source** (an export script with a step in `steps.REGISTRY`):

1. A source card in the *What goes into the archive* grid: icon, name,
   `(i)` `#q-<key>-info` (its tooltip filled by `zeigeQuellenstand`), the
   gear → `zeigeEinstellung('q-<key>')`, one chip per category (or a single
   chip with the master switch `c-<key>_enabled`).
2. A block `details.quelle-einst#q-<key>` in the settings sources card:
   essentials open, the rest under *Advanced*; summary line
   `#q-<key>-zf` shares the text of the card's `(i)`.
3. An option in the search source select (`#f-source`), a label in
   `ORDNER_ALLE` if its "folder" has a name of its own, an icon in
   `quellIkon`.
4. A row in the schedule card (`s-<key>`) when it can run on the schedule.
5. A tile in Overview when it has a count, a legend colour only if it is
   communication.
6. Strings in all three language files: `export.<key>`, `export.cat.*`,
   `search.source.<key>`, `search.folder.all.<key>`, `sched.<key>` (+`.i`),
   `settings.<key>.title`, `job.step.<key>`, `job.start.<key>`, `run.<key>.*`.

**A new setting**: a `.feldzeile` in the matching topic card. Essential
(decides *what* comes in) → open; everything else → *Advanced*. Boolean →
`SCHALTER`, number → `ZAHLEN`, text → `TEXTE`; anything handled by hand
goes into the exception list of `test_jedes_feld_ist_auch_gelistet` with a
reason. Every row gets an `(i)` with a `settings.<key>.i` text that says
what changes when you flip it. No prose paragraphs between rows.

**A new state or status**: a dot with a word next to the thing it
describes — in the settings sub-navigation, in the `(i)` of a source
card, in a `.stand` next to a field. Not in the header, not in a toast, not in the
log alone.

**A new action**: a `.mini` button in the `.aktionen` row of the group it
acts on. If it takes a while, its result text goes into the `.small.muted`
span at the left of that row (the `ABGLEICH` pattern). Only one `.act`
per screen.

**A new run or job**: nothing to add on the page — the run window renders
from `jobs.job.steps`, the console from the log, the runs table in
Overview from `/api/runs`. Give the step a `job.step.<key>` label and a `job.start.<key>`
line.

**A new view onto the archive** (like calendar or files): a `.sicht` tab
under the search, a `#sicht-<name>` block, a branch in `sicht()`. Never a
top-level tab.

**Explanations**: on the `(i)`, in `data-i18n-title`. A sentence next to a
button is allowed only when it says what the button will do *right now*
(a count, a date, a warning that applies).

## 6. Copy and languages

- Every visible text comes from `lang/{de,en,fr}.json` via `data-i18n`,
  `data-i18n-title`, `data-i18n-ph`, `data-i18n-html` or `t()`. The markup
  carries German only as a stopgap. `tests/test_i18n.py` fails on a key
  missing in one language and on a text nobody uses.
- Keys are namespaced after the place: `export.`, `search.`, `settings.`,
  `stand.`, `lauf.`, `ana.`, `sched.`, `mcp.`, `plan.`, `report.`,
  `wizard.`. Keys built at runtime (`'progress.unit.' + x`) must be listed
  in `DYNAMISCH` in the i18n test.
- Everyday words on the surface, the technical term in the `(i)` or the
  tooltip: *Access* not *token*, *AI* not *embeddings*, *searchable* not
  *indexed*. `PRUEFUNG_KACHELN` checks the header chip for this.
- Labels name the thing, not the mechanism: *Which folders are exported*,
  not *Folder rules*.
- Numbers are formatted with `toLocaleString(LOC)`; dates with `fmt()`.

## 7. States and feedback

- Loading: a `.hint` line with `cal.loading`, replaced in place.
- Empty: a sentence that says what would fill it (`files.none`,
  `ana.runs.empty`), never a bare "no data".
- Error: the server's message through `mtext()` in a `.banner.err` or the
  `.err` colour, in place — not an `alert` unless the page has no place
  for it (job start refusals).
- Unavailable: greyed with a reason in `title`, not hidden. Whoever never
  sees a possibility never learns it exists (`.aus`, `disabled`).
- Unsaved settings: the sticky save bar with *Unsaved changes*, the
  primary button, *Discard*.
- Progress: in the run window, bar plus step list; a step without a known total moves striped
  instead of inventing a percentage.
- The status poll every 2.5 s must never overwrite what someone is typing:
  fill fields once (`cfgGefuellt`), rebuild tables only when their
  fingerprint changes (`notizbuchKennung`, `wizardKennung`).

## 8. Guards

These tests encode the guide; adapt them consciously, never delete them:

- `test_die_reiterzeile_bleibt_kurz` — four tabs, views under the search.
- `test_kopfleiste_zeigt_nur_den_zugang` — header chip only; AI and MCP
  dots in the settings navigation.
- `test_archivseite_zeigt_zeiten_je_quelle_und_keinen_datenordner` — the
  archive page stays bare: state in the `(i)`, no runs, no notice, the
  log inside the run window.
- `test_jeder_id_selektor_trifft_ein_element` — every `el('x')` exists.
- `test_jedes_feld_ist_auch_gelistet` / `test_jedes_gelistete_feld_gibt_es_auch`
  — every `c-*` control is saved.
- `test_jede_einstellung_hat_eine_erklaerung` — an `(i)` per row.
- `test_erklaerungen_im_exportreiter_stehen_am_infozeichen` — no prose
  where an `(i)` belongs.
- `test_kein_stylesheet_zieht_ein_infozeichen_auseinander` /
  `test_infozeichen_behaelt_seine_groesse` — the `(i)` keeps its shape.
- `test_kein_feld_sucht_von_selbst` — filters count, they do not search.
- `test_lauffenster_bleibt_bis_zum_schliessen` — the run window opens with
  the run, stays until *Close*, minimises into the pill.
- `test_keine_verwaisten_texte` / `test_jeder_verwendete_schluessel_ist_uebersetzt`.

## 9. Before you change the interface

1. Which door does it belong to? Build, search, overview or settings.
2. Is there an existing component for it? Use that class.
3. Where is its state shown, and is it next to the thing it describes?
4. Is the explanation on an `(i)`?
5. Are the three language files updated, and does the i18n test pass?
6. Does the page still have one primary action per screen?
7. Run `pytest tests/test_app.py tests/test_i18n.py -q` and `ruff check .`.
