# Design guide

The rules the interface in `page.html` follows, and where a new element
goes so that the page still looks like one piece afterwards. Read this
before touching markup, CSS or the strings in `lang/`. What the app does
is in the README, how a piece is built is in the code, and every rule
here has a test that fails when it is broken (`tests/test_app.py`,
`tests/test_app_faelle.py`, `tests/test_i18n.py`, and the browser tests in
`tests/ui/`, which drive the real page) — adapt those tests consciously,
never delete them.

## 1. Three doors

The app does three things, and the header says exactly that:

- **Build archive** — everything about getting data in.
- **Explore archive** — everything about getting data out: search,
  calendar, address book, files.
- **Cases** — what someone keeps around one matter, for months, across
  sources.

Two smaller rooms sit at the right: **Insights** (what the archive holds)
and **Settings**, and beside them **Help**, a button that opens the
tour's window. Nothing else becomes a tab; a view onto the archive is a
way inside a door, never a door.

**The doors stay bare.** A door carries what the action at hand needs —
the state in one line, the controls, the run window while something runs
— and nothing else: no counts, no history, no notice, no prose. Whatever
explains goes into an `(i)`; whatever happened goes into Insights;
whatever can be configured goes into Settings.

**One primary action per screen**, blue, the thing a first-time user
should press: *Update archive now*, *Search*, *New case*, *Check now*,
*Save settings* (only while something changed).

**State is shown where it is fixed**, never in a status bar: the access
and the open profile in the header's one frame, AI and MCP as dots in
the settings navigation, how current the archive is on the archive
door, counts and runs in Insights, a source's state in the `(i)` on its
card, a running job in the run window.

The header holds three kinds of things, one shape each: the navigation
(tabs), one state frame (`.zustaende`, dots or icons plus a word), one
icon button (*Quit*). No pills, no third radius; it stays at the top
while the page scrolls.

## 2. Rules

Each rule is one sentence and the reason behind it.

- **One search.** A view never grows a search of its own; it hands over
  to Search with its source and filters set (one `.ghost` at the right
  of its tool row). Cases, saved searches and the history then have one
  home, and nothing is built twice. A link from outside lands there too:
  `#item=<key>`, the link of a citation over MCP, opens the item in Search.
- **A view's tool row** has its controls at the left and the one
  hand-over at the right, nothing above it that does not belong to it.
- **Filters count, they do not search.** Every filter is a pill with its
  control in a popover under it; a set pill carries its value and a `×`;
  *Clear filters* appears only when one is set. A filter that can narrow
  nothing (one folder, one type, no case, no addresses, a source without
  mail lines) is not offered — absent, never greyed out. One question is
  one pill even when it has several fields: the date range has two, the
  mail filter four, and each pill's `×` clears all of them.
- **Pickers are popovers under the thing they change** — the month's
  name opens the month picker, a pill opens its control — never a
  second window and never a field next to the thing.
- **One thing, one dialog.** What has several settings is edited in one
  window from wherever it appears: a saved search (name, case, folder,
  automatic) opens the same dialog under *Explore* and in the case, and
  its rows carry only *Run*, *Edit* and what acts now. No rename
  prompt, no switch in a row.
- **Facts, not prose.** A detail shows only what its kind of item is
  known by; an empty fact is no row, never "none"; a value that is also
  a filter is a link. Nothing about the archive itself in a detail –
  the checksums of a file sit in one closed fold at its very bottom,
  quiet unless Microsoft's disagrees.
- **Mention once, fold the rest.** A conversation is one closed fold
  under the content, not a button and a line in the head as well; so are
  an item's versions – only where it has more than one. A chosen version
  takes the content area with one bar above it – which version, *This
  version* or *Changes to the current one*, *Back to current* – and
  nothing else changes.
- **An empty state can do something.** Before the first search the last
  searches and the saved ones stand as rows, one click runs them; an
  empty list says what would fill it, never a bare "no data".
- **Unavailable is greyed with a reason** in `title`, never hidden —
  whoever never sees a possibility never learns it exists. The one
  exception is what the kind can never have (a file has no conversation).
- **Explanations live on the `(i)`** (`data-i18n-title`), or as the one
  sentence at the bottom of a popover — never both, and never a
  paragraph next to a button. A sentence beside a button is allowed only
  when it says what the button will do right now.
- **Workflows are chapters of the tour**, never text on the doors; a
  tour explains and changes nothing.
- **Every process is a run** and speaks in the run window; nothing
  writes a state of its own next to its button.
- **The app moves no data.** A setting that changes a path takes effect
  after a restart; a case points at items, it copies nothing before an
  export.
- **The status poll never overwrites what someone types.** Fields are
  filled once, tables redrawn only when their fingerprint changes.

## 3. Anatomy

**Build archive**: a status card with one line and the primary button
(the three-step first start on an empty archive), then one source card
per source — name, `(i)` with the state, gear, chips for its categories,
nothing below the chips but an actionable warning.

**Explore archive**: first the strip of the four ways (`#sichten`, with
icons). *Search* owns the search row — one field with the mode switch
inside it, one button, two icons for history and saved searches — the
pill row, the empty state before the first search (`#suche-anfang`), and
the result in three views — the case's strip, *List · Timeline · People*
(`#result-views`), nothing beside it: the list at the left, the chosen hit at the right with head, title, facts, one
action row, content, the versions fold and the conversation fold; the timeline and the
people over the whole result, up to the cap a case's list has. *Calendar*
(week, month, reconstructed appointments marked in the grid; the month
name as picker, *Today*), *Contacts* (list or picture as a segment — the
picture is the case's, over the archive — then all, from contacts, from
communication as chips, absent in the picture) and *Files* (the mirrors
as a tree) keep their own controls and hand over with one button.

**Cases**: the overview at the left (name, status, counts, closed cases
behind a link; narrowed to names while a case is open) and the case at
the right: head, the strip of three views (*Folders · Timeline ·
People*), the tool row (filter field, *Search in this case*, fold
buttons), the folds, and a foot in two groups (export left, close and
delete right). Every heading in a case is a fold, closed when the case
opens. Anything that puts items into a case goes through the one choice
window. An item that changed since it came in carries the mark and one
*Compare*, which opens the versions in a window, the version it came in
with chosen; a closed case says in one line under its head that its
state was recorded, and whether the chain holds it and a service signed
it.

**Insights** has the shape of **Settings**: a side navigation, one card
per entry, the two checks with a dot on their entry – the archive check
ends in the *Evidence* row, the chain's state and its deviations.
Settings: Microsoft Access, Sources, Schedule, AI, Claude, Profiles,
Evidence, App, Expert mode; each
source a block with the essentials open and the rest under *Advanced*;
the save bar appears only with unsaved changes. Access saves on its own
button, like the MCP card: a key is pasted and applied, not collected
with the rest.

**The run window** is the one place for a running process: headline,
progress, the steps, the log with *Copy* and *Report a problem*,
*Cancel*; it stays until closed and minimises into the header frame.

## 4. Tokens

Colours are CSS variables on `:root`, with a dark set under
`prefers-color-scheme: dark`; never a literal colour in markup or JS. The
one exception is the brand mark in the header, which carries its own two
colours in the SVG.

| Variable | Light | Use |
|---|---|---|
| `--bg` / `--card` | `#f6f7f9` / `#fff` | page ground / cards, header |
| `--ink` / `--muted` | `#1b1f24` / `#5b6570` | text / secondary text, icons |
| `--line` | `#dfe3e8` | borders, dividers |
| `--accent` / `--accent-weich` | `#2f6fed` / `#eaf0fe` | primary, active, links / selected background |
| `--ok` / `--warn` / `--err` | `#1a7f4b` / `#a2650a` / `#b3261e` | dots, tags, messages |
| `--code` | `#f1f3f6` | code, tags, bars |

Type `15px/1.5 system-ui`; headings small and heavy (`h2` 15 px, `h3`
14 px, weight 650); nothing larger than 18 px except KPI values. `.small`
13 px, `.muted`, `.num` for compared figures. Spacing: 24 px page gutter,
16 px between cards, 18 px card padding, 6–8 px between chips. Radii: 12
px cards, 10 px inner boxes, 8 px buttons and fields, 999 px chips and
pills. Icons are inline SVG from the one `IKON` set (16 px in rows, 12 px
in chips), `data-ikon` in markup, `ikon()` in generated HTML; no emoji,
no icon fonts.

## 5. Components

Use the existing class; do not invent a sibling that looks almost the same.

| Component | Class | Notes |
|---|---|---|
| Card | `.card` | white, 1 px line, 12 px radius |
| Primary / secondary / small button | `button.act` / `.ghost` / `.mini` | one `.act` per screen |
| Icon button | `.ikonknopf` | 32 px square, name in the tooltip |
| Chip (choice / segment) | `label.chip` / `.modi button` | exactly one `.on` in a segment |
| Filter pill | `.pill` in `.filterzeile` | `.wert` names the filter or its value, `.on` while set, `.x` clears, the `.popover` below holds the control |
| Popover | `.popover` | under the thing it changes: a control, one `.satz`; `.picker` is the month picker |
| Way strip / view tab | `.sichten .sicht` | the four ways of the door (`#sichten`), the three views of a case; exactly one `.on` |
| Hit row | `.hit` | tick, icon, title with the case mark, date, who with marks, preview; `.on` when chosen |
| Detail | `#detail` | `.dkopf` (kind, marks, the `.zaehler` with arrows), `.dtitel`, `.fakten`, `.daktionen`, `.dinhalt`, `details.verlauf` for the versions (`#versions-fold`) and the conversation |
| Version | `.version-bar`, `.version-body` | the bar over a chosen version; `del`/`ins` in the body with the `--diff-*` tokens |
| Empty state | `.leer` | two `.card.liste` groups of `.hist` rows with a `.gruppe-kopf` each |
| History row | `.hist` | title, criteria as `.tag`s, time or last run; with `.knoepfe` in the windows |
| Tag / mark | `.tag`, `.tag.extern`, `.tag.weg`, `.tag.mcp`, `.im-fall` | source, external, deleted – and changed since taken into a case –, via MCP, in a case |
| Status dot / state line | `.dot.ok/.warn/.err` / `.stand` | always next to a word |
| Info | `.info` | 17 px circle; text in `data-i18n-title` |
| Setting row | `.feldzeile` | label with `(i)` left, control right; `.kipp` for every boolean |
| Modal | `.modal` via `modalKopf` + `modalFuss` | cross top right, primary bottom left; what it is for goes into its `(i)`. A setting never lives in one: it is a card (`#zugang-karte` was the last exception, until 12.0) |
| Fold | `details.gruppe` / `.quelle` | every heading in a case; chevron, icon, name, count |
| Activity band | `.aktivitaet` | one bar per month or week, a click narrows the rows; `activityBand()` draws it for a case and for a result |
| Timeline row | `.zeit` | tick, date, icon, title, who; with its actions in a case, opening the detail in a result |
| People picture | `.bild` | you at the left, four `.periode` columns of `.knoten` by last contact, the chosen one's `.person-karte`; `peoplePicture()` draws it for a case, a result and the address book |
| Tool row | `.werkzeugzeile` / `.calbar` / `.dateien-kopf` | a view's controls left, its hand-over right |
| Run window | `#lauf-overlay` | progress, steps, log, *Close* / *Cancel* |
| Balance / archive row | `.bilanz .zeile` | icon, name, one sentence with numbers, one `.mini` per kind of finding |
| Tour | `#tour` | the cut-out and the card beside it; steps are data in `TOUR` |

## 6. Where a new element goes

- **A new source**: a source card, a settings block, an option in the
  source select, an icon in `quellIkon`, its strings, a check step. The
  README lists the sources; `steps.REGISTRY` the steps.
- **A new setting**: a `.feldzeile` in the matching card, essential ones
  open, the rest under *Advanced*, an `(i)` with a `settings.<key>.i`
  text that says what changes. Settings are one running page: the
  navigation beside it jumps to a card, and no setting opens a window.
- **A new state**: a dot with a word next to the thing it describes.
- **A new action**: a `.mini` in the `.aktionen` row of the group it acts
  on; a process starts through `run()`.
- **A new view onto the archive**: a `.sicht` in the door's strip, a
  `#sicht-<name>` block, a branch in `sicht()`, one hand-over button.
- **Something about a case**: a fold, a `.mini` in the row it acts on,
  and its own collection under `/api/v1/cases/{id}/` whose every write
  answers with the whole case – or, asked with `Prefer: return=minimal`,
  with the status and `ETag` alone, which the page does only where it
  wants no more than a new id; items enter a case through the one
  choice window.
- **Something of the app, not a profile**: `profiles.json` in the app
  folder, its own route, never `app_config.json`.

## 7. Copy and languages

- Every visible text comes from `lang/{de,en,fr}.json` (`data-i18n`,
  `-title`, `-ph`, `-html`, `t()`); the markup carries German only as a
  stopgap. Keys are namespaced after the place (`search.`, `cases.`,
  `settings.`, `tour.` …); keys built at runtime are listed in
  `DYNAMISCH` in the i18n test.
- Everyday words on the surface, the technical term in the `(i)`:
  *Access* not *token*, *AI* not *embeddings*, *searchable* not *indexed*.
- Labels name the thing, not the mechanism: *Which folders are exported*,
  not *Folder rules*. A control says what happens: *Search*, *Add to
  case*, *Open original*.
- Numbers through `toLocaleString(LOC)`, dates through `fmt()`.

## 8. States and feedback

- Loading: a `.hint` line, replaced in place. Empty: a sentence that says
  what would fill it. Error: the server's message in a `.banner.err` or
  the `.err` colour, in place. Where the page has no place for it — a
  refusal of an action just clicked — it goes to the one message as
  `.meldung.err`, which stays until it is read away. No `alert`: the
  browser dialog stops everything and looks like nothing else here.
- Progress in the run window: bar plus step list; a step without a known
  total moves striped instead of inventing a percentage.
- Guidance: the tour, started only by the user or offered once after the
  first run; never a permanent hint.
- Toast (`.meldung`): the one fixed message of the page, for what just
  happened somewhere the eye is not; `.err` for what was refused, and
  every refusal of the API goes through one function (`apiFehler`).

## 9. Before you change the interface

1. Which door does it belong to, and which way inside it?
2. Is there an existing component for it?
3. Where is its state shown, and is it next to the thing it describes?
4. Does it search? Then it hands over to Search instead.
5. Is the explanation on an `(i)` or one sentence in a popover?
6. Are the three language files updated?
7. One primary action per screen still?
8. `pytest tests/test_app.py tests/test_i18n.py -q` and `ruff check .`;
   `pytest -q tests/ui` where the change is one a browser would show.
