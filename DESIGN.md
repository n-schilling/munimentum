# Design guide

How the interface in `page.html` is built, and where a new element goes so
that the page still looks like one piece afterwards. Read this before
touching markup, CSS or the strings in `lang/`. The rules are short on
purpose; the reasoning behind each is one sentence.

## 1. The one idea: three doors

The app does three things, and the header says exactly that:

- **Build archive** (`nav.export`) — everything about getting data in:
  the state in one line, the sources, the run window.
- **Search archive** (`nav.search`) — everything about getting data out:
  search, filters, hits, calendar, address book, files.
- **Cases** (`nav.cases`) — what someone keeps around one matter, for
  months, across sources: the case list, one case with its casebook,
  folders, items, lists and searches, its timeline and its people, and
  the export.

Two smaller rooms sit at the right of the header: **Insights**
(`nav.analytics`, what the archive holds) and **Settings** — and beside
them **Help** (`#nav-hilfe`), a button, not a tab: it opens the tour's
window (`hilfeFenster`), one row per chapter and the full tour, and
leads nowhere else. Nothing else becomes a top-level tab; `tests/test_app.py::test_die_reiterzeile_bleibt_kurz`
counts five and fails on a sixth. Cases became a door in 11.0 because a
case is neither building nor searching and has a primary action of its
own; a view onto the archive (calendar, files) is still a `.sicht` under
the search.

**The doors stay bare.** Build archive, Search archive and Cases carry only
what the action at hand needs: the state in one line, the controls, and
while something runs, the run window or its pill. No counts, no history,
no schedule line, no notice, no prose. Whatever explains goes into an `(i)`; whatever
happened goes into Insights; whatever can be configured goes into
Settings — including the notice about a newer release, which sits under
*Settings › App* with a dot on that entry. The search history and the
saved searches are windows behind two `.ghost` buttons in the search row,
not a second row on the door. If a new element would add a
line to one of the doors, it belongs somewhere else.

Every screen has **one primary action**, blue, and it is the thing a
first-time user should press. Build archive: *Update archive now*. Search
archive: *Search*. Cases: *New case*. Insights: *Check now*. Settings:
*Save settings* (only shown when something changed).

State is shown **where it is fixed**, not in a status bar:

| State | Where |
|---|---|
| Access (token / sign-in) | the state frame in the header (`#zustaende`), click opens the wizard |
| Which profile is open | the state frame in the header (`#pill-profil`), only while there is more than one profile; click opens the switch window (`profilWechselnFenster`), whose gear leads to *Settings › Profiles* |
| AI (Ollama) | dot next to *AI (Ollama)* in the settings sub-navigation |
| Claude (MCP) | dot next to *Claude (MCP)* in the settings sub-navigation |
| How current the archive is | the one line of the status card on *Build archive* |
| Counts, runs, completeness | Insights |
| Per source: folders chosen, last run | the `(i)` on that source's card, and the summary line of its settings block |
| A running job, and its log | the run window (`#lauf-overlay`), open until the run is done and closed by hand; minimised, the run state in the header frame |
| A newer release | banner under *Settings › App*, dot on the *App* entry |

The header has three kinds of things and one shape for each:

- **Navigation**: the five tabs. The three doors as underlined tabs, the two
  side rooms as small boxed tabs (`.nav-neben`), and *Help* beside them
  in the same shape — a button that opens a window, never a tab. Nothing
  else becomes a tab.
- **State**: one frame (`#zustaende`, 8 px, one line) holding every state
  that must be seen from every page, each a `.zustand` — a dot or an icon
  plus a word — separated by hairlines, and each a click to the place
  where it is fixed. Today: a minimised run (`#lauf-pille`, only while one
  exists), the open profile (`#pill-profil`, only while there is more than
  one) and the access (`#pill-token`). A new state that truly belongs
  up here goes into this frame as another `.zustand`, never beside it.
- **Action**: one icon button (`.ikonknopf`, 8 px) — *Quit*. A second one
  is allowed only for an action that must be reachable from every page;
  it sits next to it, same size, name in the tooltip.

No pills, no third radius: everything up here is text, or 8 px. The
header is sticky (`position: sticky; top: 0`): the doors, the state frame
and the pill stay in reach however far the page scrolls, and what the
page scrolls to (`scroll-margin-top`) or pins below it (`#detail`,
`.snav`) keeps clear of it.

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
   button, and the two `.merker` icons (history, saved searches).
2. Filter row (`#filter`): always visible, every filter a `.pill` with
   its control in a `.popover` under it — the select's options as a
   `.wahl` list with counts, the date with `.schnell` ranges, the person
   field with its suggestions, one `.satz` of explanation at the bottom.
   A set pill carries its value and a `.x` that clears it; *Clear
   filters* and the `.stand` count appear only when one is set. The
   controls keep their ids (`#f-…`): the criteria, the history and the
   saved searches read them.
3. View tabs (`.sicht`): Hits, Calendar, Address book, Files.
4. Hits: list on the left, the selected hit on the right: head
   (`.dkopf` — kind, marks, the `.zaehler` place in the list with
   arrows), title, the `.fakten` this kind of item is known by (a value
   that is also a filter is a link), one `.daktionen` row (open
   original, add to case, find similar), the content, and for mail and
   chat the conversation as `details.verlauf`. The AI answer sits above
   the list and cites into it. Every hit row starts with a tick (`.wahl`, greyed with its
   reason on an index without keys) and carries the case mark (`.im-fall`)
   in its title when it sits in a case; the list head (`.liste-kopf`)
   names the count and offers *Add all … to a case*, the selection bar
   (`.auswahl-leiste`) appears only while something is ticked.

The search row's two `.ghost` buttons open the **history window** and
the **saved searches window** (`.modal.breit`, one `.hist` row per
search: title, criteria as `.tag`s, time or last run, the `.mini`
actions), each drawn as one string like every window. The *Cases* filter
(`#f-fall`) is a pill like the folder, its select in the popover, filled from
`/api/faelle` and shown only while there is a case to choose – a filter
that can narrow nothing is not offered, the rule the folder and type
selects follow. The *parties* filter (`#f-party`: internal and external
· internal only · with external parties) follows the same rule: it
appears once the index carries the parties' domains. A hit whose
parties reach outside the internal domains carries `.tag.extern` in
its who line and in the detail's head.

**Cases** (`#tab-faelle`): section head with the name, its `(i)` and
the one `.act` (*New case*) — no sentence beside them; then
`.faelle-split`: the **overview** at the left and the case at the right.
The overview (`.card.liste.seitenleiste`) has a `.liste-kopf` with a
quiet uppercase label and one `.ikonknopf` arrow, nothing else — no
count, no filter; `.fall` rows (name, `.tag.offen`/`.tag.zu`, one icon +
count per source, one muted line); closed cases behind the *Show n
closed cases* link in its `.fuss`. Opening a case adds `.eng` to the
split: the overview narrows to the names, the open one bold, and the
arrow widens it again (`leiste()`). The case card is a static frame the
JS fills: `.fallkopf` (`.titelzeile` with `h2`, status tag, *Edit…*;
`.meta`, one sentence of facts; `.beschreibung`), the `.werkzeugzeile`
(the filter field `#fall-filter` with its `.stand`, *Search in this
case*, and `.falten` at the right: expand all, collapse all, *New
folder…*), the sticky `.auswahl-fall` bar while rows are ticked (count,
*Move to…* select, *Remove from case*, *Clear selection*),
`#fall-inhalt` with the folds, and the foot `.aktionen.fuss-zwei` in
two groups (`.gruppe-links`: *Export case…*, last export and *Show
folder*; `.gruppe-rechts`: *Close case* / *Reopen*, *Delete case*).

Every heading inside the case is a native `details.gruppe`, closed when
the case opens (`FALL_AUF` keeps what the user opened across redraws),
its `summary` carrying the chevron, the icon, the name, the count and
the `(i)`; a folder's summary also carries `.werkzeuge` (*Search here*,
*Rename*, `×`) that show on hover or while open. With folders the items
are one fold per folder in case order plus *Unsorted* last, inside which
the sources fold again as `details.quelle` (the first one open);
without folders, one fold per source. Rows: `.notiz`, `.hist`,
`.eintrag` with a tick, thirty per source then *show n more*. The filter
(`fallFiltern`) is client-side over the loaded case: it redraws the
folds with the matching rows only and says "n of m shown"; a new filter
text opens every fold once (`alleAuf`), after that the folds are the
user's again, collapse all included.
*New folder…* and *Rename* open a window (`ordnerFenster`), never an
inline field; *Move to… › New folder…* opens the same window and moves
the ticked rows once the folder exists. A closed case draws no tick, no
add field and no folder tools, and hides *New folder…*. Every write
goes to `/api/faelle/*` and comes back as the whole case; the page never
guesses what a write did. The *via MCP* tag (`.tag.mcp`) sits on the
item's title and on the note's date line — never on a folder, a source
or the case.

**Three views, one strip.** Between the case head and the tool row sits
`.sichten.fall-sichten` — the sub-view strip the search page uses —
with `data-fallsicht` tabs *Folders*, *Timeline*, *People* (the people
tab carries its count). `fallSicht()` swaps only what lies between the
tool row and the foot: `#fall-inhalt` (the folds above),
`#fall-zeit-sicht` (the activity band `#fall-aktivitaet` and the rows
`#fall-zeit`) or `#fall-personen`; head, filter field, move bar and foot
stay. The
fold buttons and *New folder…* (`#fall-werkzeug-ordner`) belong to the
folder view; the timeline shows one select for its direction
(`#fall-werkzeug-zeit`) instead. The **timeline** is drawn from the
loaded case: items by the "YYYY-MM-DD HH:MM" the index gave them and the
casebook's notes in between (`.zeit.notiz-zeit`, greyed), under
`.zeit-monat` headings, undated items last, fifty rows then *show n
more*; a `.zeit` row has the tick, the day and time, the icon, title,
who and the folder as a `.tag`, the remark, and the same actions as an
item row. Above the rows sits the **activity** (`.aktivitaet`): one
`.monat` bar per month – per week when the case spans less than 92 days
– its height the items of that bucket against the busiest one, the
notes as `.punkte` dots, the axis of short names below and one
`.auswahl-zeile` that either sums the case up or names the chosen
bucket with *Show all*; a click on a bar (`zeitEimerWaehlen`) narrows
the rows to it, a second click lets go; the bars grow in when the view
opens, off under reduced motion. The **people** are counted from what
the items say (`wer`, split on ", ") and drawn as a picture (`.bild`):
you at the left (`.ich`: the initials of *own_name*, else the sign-in's
name, else the account's first letter; the address below), then four
`.periode` columns by the person's last contact – this week, this
month, this quarter, older, tinted from the accent to grey – each
person a `.knoten` button with a circle sized by the square root of
their item count against the busiest, initials inside, a dashed ring
and the `.tag.extern` mark for an address outside the internal domains,
the name and "n items · last contact" beside it; every second person
steps 14px to the right, the people rise in when the view opens. A
click (`fallPersonWaehlen`) opens the one `.person-karte` under the picture
with the address, the per-source counts, the span of dates, *Timeline*
(the timeline with the filter set to the name) and *Search* (the search
page with the person and case filters set); a second click closes it. The
address comes from the index (`who_mail`, mails and appointments) with
the case on `/api/faelle/fall`; *external* is any address outside the
setting *internal_domains* (else the signed-in account's domain), and
the user is left out by the account address or the name (*own_name*,
else the sign-in's). The filter field narrows whichever view is open
and says "n of m shown" for it.

**The remark** (`.bem`): one marked line under the item's title, in the
folder view, the timeline and the export; the pen on the row
(`.mini.nur-ikon`) opens a window with one textarea, *Save*, and *Remove
the remark* as a link once one exists — never an inline field, and
nothing of the item repeated in the window. **The rest of a conversation**: the add-to-case
window shows one switch under the folder row when the hits have threads,
worded with the counts from `/api/thread` (how many more, how many the
chosen case holds already; the text follows the chosen radio); a row
whose conversation the case lacks part of carries *Thread +n*
(`thread_offen` on the item), one click fetches the rest into the row's
folder and says so in the toast (`.meldung`, the one fixed toast of the
page, `meldung()`). The hit's detail names its conversation in one fold
under the content once it is fetched ("Part of a conversation of n
messages"; open, the rows the case holds carry a dot, and *Add the other
n* fetches the rest).

The export window is the title with its `(i)` (what the ZIP holds and
where it lands), a `pre.export-inhalt` overview — the ZIP's name, the
index, timeline, CSV and casebook lines, one line per folder with count
and sources — and one button; no switch, no gear. The choice window
(`.wahl-liste`) has a folder row below the cases: a select with the
chosen case's folders, *Unsorted* first and *New folder…* last (a text
field appears for the name); a new case offers only those two.

**The run window** (`#lauf-overlay`, the wizards' frame, wider):
everything about a running process — headline with step and start time,
progress bar, step list, the log (`#protokoll`) with *Copy* and *Report a
problem*, *Cancel*. **Every process is a run and starts through `run()`**:
the export, *Sync now*, *Force full sync*, *Fetch now* and *Fetch again*,
the completeness check, the archive check and each of its actions, the
folder-structure syncs. Not one of them writes a state of its own next to
its button ("Checking…", "Syncing…"): the window opens with it, its log
says what happens, and Insights reloads its numbers when the run is
done. It opens when a run starts here or when the page is
opened while a run is on (by hand or by the schedule); a scheduled run
that starts while someone works on the page appears as the pill first. It
stays until the run is done, then shows the result (headline with count
and duration, the steps with their numbers) until *Close*. *Minimise*
hides it and shows the run state in the header frame; a click there reopens it. It
sits below the wizards' overlay, so an expired token can still ask on
top.

**Insights** (`#tab-analytics`): the same shape as Settings – a
sub-navigation on the left (`.snav`, `#ana-nav`), one card per entry on
the right, in the order of the navigation: Key figures, History,
Completeness, Archive and bookkeeping, Runs. The two checks carry their
state as a dot next to their entry (`#p-ana-check`, `#p-ana-archiv`:
warn while something is open or found, ok when all agrees, none before
the first check). Read-only apart from *Refresh* and *Check now*. The
completeness card is one button and one **balance row** (`.bilanz
.zeile`) per source in use,
drawn from `steps.PRUEFUNGEN` in that order: icon, name, one sentence with
the four numbers (here · not fetched yet · deliberately excluded · deleted
but kept; mirrors add "waiting for their cadence"), when it was checked,
and *Fetch now* only while something is open. The units with something
open sit collapsed below the row; excluded units are never named. A used
source without a report says "not checked yet", an unused one without a
report is not drawn. Below it the **archive and bookkeeping** card: the
inward check (`archive_check.py`, no access needed), a `.ghost` button
since the screen's primary is *Check now*, the same row shape per export
folder with five file counts (agree · missing · incomplete · without
bookkeeping · lost) and one row for the index against the archive, whose
only action is *Index only*. A row carries one `.mini` per kind of
finding it has, and none otherwise: *Findings…* (the window `befunde`,
in the export list's shape: one `details.plangruppe` per kind, *Copy*,
*Open folder*), *Fetch again* (`/api/archiv/nachholen`: exactly the
missing and incomplete files of the row, written to a list the source's
step reads as `FETCH_LIST` – each fetched through its own bookkeeping,
the source ticked or not, nothing listed; the run is source, index and
the archive step `pruefen`, which judges the row afresh and dates it as
fetched), *Note as lost* (lost tombstones – and files still missing
after a fetch that ran since the finding, `nachgeholt > fehlt_seit`: the
row then reads "still missing after a fetch", the note asks with the
count and takes them along), *Set aside* / *Put back*, *Rebuild
bookkeeping*. Set aside, rebuild and the note of missing files ask with
`confirm` and the count; every action goes to `/api/archiv/<aktion>` and
is a run of its own (a step of `archive_check --aktion`, the rebuild
followed by the source's export and the index) – the run window opens
with it, the log says what moved, and the row is judged afresh when the
step is done. Nothing on that card deletes; what moves goes to `_fremd/`
and comes back.

**The chooser** (`profil.html`): the one page before the doors, served
only while there is more than one profile and none was named at start. One
card per profile — name, account, last run — and the whole card is the
choice (the *Open* inside only says so), plus one switch *open without
asking*. Nothing else: no creating, no command-line hints — the chooser
chooses, everything about profiles lives under *Settings › Profiles*. It
carries the tokens and, of the app's page, only the name bar: no tabs, no
state frame, because there is no profile yet to draw a state from. Every
text comes from `profile.*` through the JSON the server injects. It is
served by `Wahl`, a `Handler` with three routes and no app behind it —
the same Host check, body cap and headers as the app.

**Settings** (`#tab-einstellungen`): sub-navigation on the left
(`.snav`), one card per topic on the right, in the order of the
navigation: Sources, Schedule, AI (Ollama), Claude (MCP), Profiles, App,
Expert mode. *Profiles* has two groups, top to bottom: *These profiles
exist* — one `.urltab.abw` row per profile with name, the *open* tag and
its account (no folder: that is the group below), each profile that is
not open with its *Rename…*, then *Switch profile…* and *New profile…* in
the `.aktionen` row and the switch *open without asking* — and *Current
profile ‹name›* with its data folder, index folder, profile folder and
the application's location. The first group hides under `--data-dir`.
Each source is a collapsible block (`details.quelle-einst`)
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
| Cadence row | `.feldzeile` + `select#c-cadence-<key>` | last row of the group it paces; key = source or `source:category` in `sync_cadence` |
| Cadence departures | `.urltab.abw` + `.mini` *Pick …* | one row per unit that departs from the cadence above it: path, select, `×`; keys `source:category:<path>` |
| Tour | `#tour` with `.tour-loch` + `.tour-karte` | coach marks: the cut-out dims everything but the step's element, the card next to it carries chapter · step counter, title, one sentence, *Next* / *Back* / *Skip*; steps are data in `TOUR`, texts `tour.<chapter>.<step>` |
| Cadence window | `.baum` inside `.modal.breit` | the unit tree of the last list sync: filter, *Only set* chip, one row per unit with indent, count, tags (`n subfolders`, `n set below`, `not exported`) and ONE select (`.erbt` "as above · …" or `.gesetzt` with `×`); roots show the general cadence as text |
| Action row | `.aktionen` | status text left, small buttons right |
| Tag | `.tag` | source, deleted, origin; `.tag.weg` for deleted |
| Status dot | `.dot.ok/.warn/.err` | always next to a word, never alone |
| State line | `.stand` | dot + word, next to a field |
| Info | `.info` | 17 px circle with an `i`; text in `data-i18n-title` |
| Banner | `.banner`, `.banner.warn`, `.banner.err` | one sentence, optional link |
| Modal | `.modal` via `modalKopf(title, kind, info, extra)` + `modalFuss` | cross top right, primary action bottom left; what the window is for and where data stays goes into the `(i)` at the title, not into paragraphs; `extra` is one `.zahnrad` next to the cross when the window has settings elsewhere (the switch window) |
| Wizard state | `.banner` with `.dot` + one sentence | "Connected as … — valid for another …"; nothing about what happens next |
| Console | `#log`, `.lauflog` | dark, monospace, 12 px, lines coloured by level; in the run window only the current run's lines (from the job's `log_seq` on) |
| Step list | `.schritte .schritt` | icon, name, detail, duration |
| Run window | `#lauf-overlay` / `.lauf-fenster` | modal frame; `#fortschritt` while running, `#lauf-ergebnis` afterwards, `#protokoll`, footer with *Close* / *Cancel* |
| Header state | `.zustaende .zustand` | dot or icon + word inside the one frame; `.lauf-pille` adds the `.mini-balken` |
| Icon button | `.ikonknopf` | 32 px square, 8 px radius, name in the tooltip; the header's *Quit* |
| Help window | `.hilfe-liste .hilfe-kapitel` | one button per tour chapter (number or check, icon, name, one line with the step count, *up next* / *seen*), the full tour as the window's one primary action |
| KPI tile | `.kpi` | value, title, hint; `.klickbar` when it leads somewhere |
| Balance row | `.bilanz .zeile` | icon, name, `.dot` + one sentence (`bilanzSatz`), `.wann`, *Fetch now* while open; a `details` with the open units below |
| Hit row | `.hit` | tick (`.wahl`, under `#results` only), icon, title with the case mark, date, who with the `.tag.extern` mark when a party is outside the internal domains, preview; `.on` when selected; also the rows of the switch window (`.modal .hits`), where `.fest` marks the open profile as shown, not chosen |
| Case mark | `.im-fall` | pill with the case icon and the name (or the count for several) on a hit's title and in the detail's head; `.zu` when every case it sits in is closed |
| List head / selection bar | `.liste-kopf` / `.auswahl-leiste` | above `#results`: count left, one `.mini` action right; the bar only while something is ticked |
| Case overview | `.seitenleiste` | the case list: `.liste-kopf` with a quiet uppercase label and the one arrow `.ikonknopf`, `.fall` rows, the closed-cases link in `.fuss`; `.eng` on the split narrows it to the names while a case is open |
| Case row | `.fall` | name + status tag, icon + count per source, one muted line; `.on` when open on the right; the name alone in `.eng` |
| Case head | `.fallkopf` | `.titelzeile` (name, status, *Edit…*), `.meta` (one sentence of facts), `.beschreibung` — three lines, no fold |
| Tool row | `.werkzeugzeile` | the filter field with its `.stand`, *Search in this case*, and `.falten` at the right (expand all, collapse all, *New folder…*) |
| Fold | `details.gruppe` / `details.quelle` | every heading in a case; the summary carries chevron `.pfeil`, icon, `.name`, `.n` count, `(i)`, and on folders `.werkzeuge`; closed when the case opens, `.quelle` is the source fold inside a folder |
| Move bar | `.auswahl-fall` | sticky while rows of a case are ticked: count, *Move to…* select (folders, *Unsorted*, *New folder…*), *Remove from case*, *Clear selection* |
| Origin tag | `.tag.mcp` | *via MCP* on an item's title or a note's date line — never on a folder, a source or the case |
| Two-group foot | `.aktionen.fuss-zwei` | `.gruppe-links` (the export) and `.gruppe-rechts` (what happens to the case) |
| Case view tab | `.fall-sichten .sicht` | *Folders · Timeline · People* under the case head; exactly one `.on`, `data-fallsicht` names the view |
| Timeline row | `.zeit` | tick, day and time, icon, title with who and the folder tag, the remark, the item's actions; `.notiz-zeit` is a casebook note in between, `.zeit-monat` the month heading |
| People picture | `.bild` | `.ich` (you) and four `.periode` columns by last contact; a `.knoten` per person: circle sized by items, initials, the `.tag.extern` mark and dashed ring for an outside address, name and "n items · last contact"; `.on` on the chosen one |
| Person card | `.person-karte` | under the picture for the chosen person: address, per-source counts, span of dates, *Timeline* + *Search* |
| Activity band | `.aktivitaet` | above the timeline rows: `.monat` bars (`.leer` without items, `.on` when chosen, `.punkte` for notes), the `.achse` of names, the `.auswahl-zeile` |
| Remark | `.bem` | one marked line under an item's title – in the item row, the timeline row and the export; the pen `.mini.nur-ikon` on the row opens its window |
| Toast | `.meldung` | the one fixed message of the page, `meldung(text)`; for what just happened somewhere the eye is not (the conversation fetched into a folder) |
| Export overview | `pre.export-inhalt` | what the ZIP will hold, one line per part and per folder, in the export window above its one button |
| Item row | `.eintrag` | tick (open case only), icon, title with the origin tag, who, date, *Open* + `×` — the items of a case under their source fold, thirty then *show n more* |
| Note | `.notiz` | when (with the origin tag), text, *Edit* + `×`; `.notiz-neu` is the add field at the end of the casebook |
| History row | `.hist` | title, `.tag` criteria in `.tagleiste`, time or last run, `.knoepfe` of `.mini`s; the rows of the history, the saved searches, a case's lists and searches; `.hist-tag` is the day heading |
| Choice list | `.wahl-liste` | one radio per open case and a *New case…* field, then the folder select (*Unsorted*, the case's folders, *New folder…*) — the one window for putting anything into a case |
| Detail | `#detail` | `#detail-inhalt` with `.dkopf` (kind tag, external and deleted marks, the case mark, the `.zaehler` with arrows), `.dtitel`, `.fakten` (only the facts the kind has; person, folder and type as filter links), `.daktionen` (original, case, similar), `.dinhalt` (iframe or the index's text) — no storage path, the original is a click away; `#detail-verlauf` holds `details.verlauf`, the conversation as one fold of `.vzeile` rows (`.drin` in the case, `.dies` the open one) with a `.fuss` |
| Side navigation | `.snav .snav-punkt` | Settings and Insights alike: one per card, `data-ziel` names the card; a `.stand` or `.dot` at the right |
| Source block | `details.quelle-einst` | summary with name and state line `.zf`, `.qinhalt`, `details.erweitert` |
| Filter pill | `.pill` in `.filterzeile` | `.wert` names the filter or its value, `.on` while set, `.x` clears it, the `.popover` under it holds the control |
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
5. A tile in Insights when it has a count, a legend colour only if it is
   communication.
6. Strings in all three language files: `export.<key>`, `export.cat.*`,
   `search.source.<key>`, `search.folder.all.<key>`, `sched.<key>` (+`.i`),
   `settings.<key>.title`, `job.step.<key>`, `job.start.<key>`, `run.<key>.*`.
7. A check: a `--check` mode of the export that writes one balance
   (`completeness.bilanz`) with the export's own notion of "excluded", a
   `check_<key>` step in the registry with the export's environment, and a
   row in `steps.PRUEFUNGEN` with its title key `ana.check.title.<key>`.

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
acts on. If it is a process, it starts through `run()` and speaks in the
run window (the `ABGLEICH` pattern); only a synchronous answer – a save
the server refuses – goes into the `.small.muted` span at the left of
that row. Only one `.act` per screen.

**A selection** (which units of a source come along): one shape for every
source — a `.feldzeile` whose label names the choice ("Which folders are
exported") with the `(i)` explaining the rule syntax and a state line
(`#<key>-state`, "n of m chosen, as of …") at the right, the rules
textarea as `.feldzeile.breit` below it, then an `.aktionen` row with
*Sync … list* (the `--<list>` mode of the export, via `ABGLEICH`) and
*Show export list* (`zeigeExportliste('<key>')`, three groups: comes
along, left out with the rule, only in the archive). Nothing else decides
what a source exports; no prose paragraph says "everything comes along".

**A cadence**: a `.feldzeile` with the `settings.cadence` label at the END
of the group it paces — a whole source (OneDrive, To Do) or one category
of it (Mail, Calendar, Contacts; the four Teams kinds under their own
*Sync cadence* heading). Every gate lives inside the export, and a skip
is one `run.cadence.skip` line in the log. *Sync now* — the button in a
URL row, or a `.mini` in the source's `.aktionen` row — lets every gate of
that run step aside once; it never changes the cadence itself. *Force full sync* —
a `.mini` in the `.aktionen` row of the source's *Advanced* group, one per
source, asked once with `confirm`, with an `(i)` beside it that says what
this source's full read takes along (`settings.full_sync.i.<key>`: the
ticked parts, the rules, what a first-export date still bounds) — starts a
run of that source alone with
`full_sync`: the export forgets its stored change pointers and reads the
source again as on its first export (`export_util.voll_neu`, one
`run.full_sync` line in the log), writing everything over and deleting
nothing. It is the way a setting that only reaches touched units
(attachments, files, images) reaches what is already archived. Between
the two sits the **resync** (`resync` on `/api/run`, `RESYNC`,
`export_util.abgleich`, one `run.resync` line): the pointers are
forgotten, the versions kept, so the source is listed once in full and
only what is not here comes. *Fetch now* in the balance (`holeQuelle`
→ `/api/bilanz/holen`, label `job.holen`) uses it only where it is the
cheapest way: the mailbox as a resync limited to the row's open folders
(`resync_folders`, `RESYNC_FOLDERS`, `export_util.abgleich_ordner`, one
`run.resync.folders` line), the mirrors by the open files' ids the check
noted in the report (`offene`, `FETCH_LIST` with `{id, rel}` pairs, the
row adjusted by `completeness.abgeholt` – a resync only when the report
capped them), every other source as its regular sync-now run; the
source's check step follows the index in the same run, so the row is
judged afresh without a second click. The resync has
no button of its own in the settings: it is the answer to a finding, not
a mode to pick. A full sync forgets the pointers as well, so
`abgleich()` is true for both. *Sync now*, *Force full sync* and *Fetch
now* on a source the settings do not tick are refused with
`srv.inactive`, naming the source – never a run that carries nothing but
the index. The archive card's *Fetch again* is different in kind: a
**targeted fetch** (`FETCH_LIST`, `export_util.nachhol_liste`, one
`run.nachholen.start` line, one `run.nachholen.done` line) that reads no
listing at all – every export answers it from its own bookkeeping
(`nachholen()` in each exporter: Outlook by id, the mirrors by drive
item, Teams by conversation, Planner, To Do and OneNote by unit) – and
therefore runs whether or not the source is ticked.

**A departure from a cadence** (one mail folder, team, channel, chat,
drive or library folder or To Do list that syncs differently): never a
second select in the block. The block gets a
*Per …* row with a state text ("n set – everything below inherits") and
the departures as `.urltab.abw` rows below it, plus *Pick …* in the
`.aktionen` row. Picking happens in the cadence window (`kadenzFenster`):
the tree of the last list sync, drillable to any depth, one select per
row that reads "as above · <inherited> (<from>)" until a value is set. A
value on a unit reaches everything below it until a deeper unit sets its
own — the rule is `export_util.kadenz_fuer` on the export side and
`kadenzWirksam` on the page, and the export list shows the effective
cadence as a `.tag`. A unit the rules leave out has no select. The window
writes rows, the save bar saves them; the exports skip per unit and say so
in ONE line per category (`run.outlook.folders_paced`, `run.teams.paced`,
`run.onedrive.folders_paced`, `run.sharepoint.folders_paced`,
`run.todo.paced`). In the drive mirrors (OneDrive, SharePoint libraries)
the departure paces downloads, not the listing: the delta stream stays
whole, files in a folder not yet due wait in state.db. A library's own
cadence is its URL row; the window shows the site as a heading and the
library as the root.

**Something that belongs to the app rather than to a profile** (the list
of profiles, which one was opened last, *open without asking*): it lives in
the app folder outside every profile (`profiles.json`), is set under
*Settings › Profiles* through its own route, never inside `app_config.json`.
The status poll carries only what the header needs (`profile`: name,
whether there are others, whether profiles exist at all); the list with
accounts, folders and last runs comes from `GET /api/profiles` when the
card or the switch window shows it. A profile is a folder under
`profiles/`, the first one `standard`; the app never copies or moves
anything into or out of one — the one exception is the layout upgrade at
start (`layout_umzug`: an archive from before 10.0 is renamed into
`profiles/standard/`, all or nothing, before any file is opened), and a
rename of a profile that is not open is a folder rename with its paths
following. Switching is a restart with `--profile` on the same port, and
the page waits for the new instance before it reloads — no in-place
repointing of a running app.

**A new run or job**: nothing to add on the page — the run window renders
from `jobs.job.steps`, the console from the log, the runs table in
Insights from `/api/runs`. Give the step a `job.step.<key>` label and a `job.start.<key>`
line.

**A new view onto the archive** (like calendar or files): a `.sicht` tab
under the search, a `#sicht-<name>` block, a branch in `sicht()`. Never a
top-level tab.

**Something about a case**: a fold in `zeichneFallOrdner`, a `.mini` in
the row it acts on, a route under `/api/faelle/` that answers with the
whole case. Anything that puts items into a case goes through the one
choice window (`fallWahl`), never a second picker. The case remembers
items by their key (`schluessel.py`), so a new source gives its records a
key in `corpus.py` or a rule in `schluessel.fuer`, and its export an
anchor the export's `index.html` can link to (`id="m-…"`, `k-…`, `t-…`).
What goes into an export lives in `case_export.py`, the page only names
the folder.

**Explanations**: on the `(i)`, in `data-i18n-title`. A sentence next to a
button is allowed only when it says what the button will do *right now*
(a count, a date, a warning that applies). **An `(i)` or a sentence,
never both**: a heading that carries an `(i)` has no explaining sub line,
and a sub line that explains has no `(i)` beside it – the same thing said
twice reads as noise, and the `(i)` is the one place where an explanation
lives. Where both exist today, the sentence goes.

**A workflow explanation** (how to click through something): a chapter of
the tour, never text on the doors. A step is one entry in `TOUR` — its
element (an id, optionally an ancestor via `rahmen`), the tab, what to
open first (a block, *Advanced*), title and one-sentence text. The tour
explains and never changes a setting. Six chapters, in the order the
full tour walks them (`TOUR_REIHE`): the archive, a source's settings,
the search, the cases (`faelle`: from the search into a case, the
overview, the case's head, views, tool row, folds and foot, then who is
external and what Claude may do; it opens the first case where one
exists, so its steps point at something), Insights (`insights`: the
figures, the gaps, the two checks, the runs) and Claude (`claude`: the
switch, the two routes, the snippet, what it may change). The **help
window** (`hilfeFenster`, `.hilfe-kapitel` rows from `TOUR_KAPITEL`:
number or check, icon, name, one line and the step count, *up next* on
the first unseen chapter, *seen* on the others) starts one chapter or
the **full tour** (`tourAlle`: every chapter in order as a chain
`TOURSTAND.kette`, the card saying "Full tour k of n"; Done goes on,
Skip or Esc ends the whole thing; the search chapter is walked past
while there is no index). Entry points: the Help button in the header,
the first-start card, the result of the first run (search chapter), the
gear on a source card (source chapter from inside the archive chapter),
the hits step of the search chapter (cases chapter as its branch) and
the empty cases door — nothing under *Settings › App* any more.
Seen chapters live in `tour_seen` in the settings; a tour never restarts
on its own. `test_rundgang_ziele_existieren` checks that every step's
element is in the markup.

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
  sees a possibility never learns it exists (`.aus`, `disabled`). The one
  exception is a possibility that can never exist for that kind of item:
  a file, a page or a task has no conversation, so the detail draws no
  fold for one – the rule of the filter that can narrow nothing.
- Unsaved settings: the sticky save bar with *Unsaved changes*, the
  primary button, *Discard*.
- Progress: in the run window, bar plus step list; a step without a known total moves striped
  instead of inventing a percentage.
- Guidance: the tour (`#tour`) — a dimmed page with one element lit and a
  card beside it, started only by the user (or offered once after the
  first run), ended with *Skip*, *Done* or Esc; never a permanent hint.
- The status poll every 2.5 s must never overwrite what someone is typing:
  fill fields once (`cfgGefuellt`), rebuild tables only when their
  fingerprint changes (`notizbuchKennung`, `wizardKennung`).

## 8. Guards

These tests encode the guide; adapt them consciously, never delete them:

- `test_die_reiterzeile_bleibt_kurz` — five tabs, views under the search;
  Help beside the side rooms is a button, not a tab, and the chapters no
  longer sit under *Settings › App*.
- `test_kopfleiste_zeigt_nur_den_zugang` — the header's one frame holds
  the run, the profile and the access, nothing beside it; AI and MCP
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
- `test_kadenzfenster_baum_und_vererbung` — the cadence window's tree and
  the inheritance rule match the exports.
- `test_jede_quelle_hat_den_vollsync_knopf_unter_erweitert` — one *Force
  full sync* per source, under *Advanced*, in an `.aktionen` row.
- `test_bilanzzeilen_passen_zum_register` /
  `test_bilanzzeile_spricht_in_saetzen_und_zahlen` — every balance row has
  its check step, folder and title; the sentence carries numbers, never
  names, and *Fetch now* only while something is open.
- `test_archivzeile_spricht_in_saetzen` / `test_archivaktionen_auf_der_seite`
  (every action and check opens the run window; the note of missing files
  only after a resync) / `test_http_archivaktionen_bewegen_nur_beiseite_und_loeschen_nie`
  / `test_http_neu_aufbauen_ist_ein_lauf_aus_drei_schritten` /
  `test_http_nachholen_schreibt_die_liste_und_startet_den_lauf` /
  `test_http_run_lehnt_eine_nicht_angehakte_quelle_ab` — the
  inward rows speak in sentences, carry one button per kind of finding,
  and the actions move files aside and back but never delete one.
- `test_insights_hat_eine_seitennavigation_je_karte` — Insights is shaped
  like Settings: one navigation entry per card, each pointing at a card
  that exists, the two checks with a dot.
- `test_rundgang_ziele_existieren` / `test_rundgang_kapitel_laufen_durch` —
  every tour step points at an element, the six chapters run through and
  are marked seen once, the help window lists them with the first unseen
  as next, and the full tour chains them in order, walking past the
  search chapter without an index.
- `test_profil_in_kopfzeile_und_speicherorten` — the profile state shows
  only with more than one profile; the switch window offers only the
  others; the group hides under `--data-dir`.
- `test_profilseite_liegt_als_datei_neben_dem_code` — the chooser is a
  data file the bundle ships, with nothing of the app's page in it.
- `test_umzug_alles_oder_nichts` / `test_umzug_laesst_eigene_pfade_in_ruhe`
  — the one move the app makes is a set of renames that either all happen
  or none, and never touches a folder the user pointed elsewhere.
- `test_die_dritte_tuer_und_ihre_teile_stehen_im_markup` /
  `test_die_seite_fuehrt_faelle_durch` (tests/test_app_faelle.py) — Cases
  is a door with one primary action, the history and saved windows sit in
  the search row, the case filter counts but does not search and lists
  the folders under their case, a hit without a key cannot be ticked,
  the choice window lists open cases only and asks for the folder, the
  overview carries no count and narrows when a case opens, every group
  is a fold closed at open, the filter narrows the rows and opens the
  folds, ticked rows move through the bar, folders are made and renamed
  in a window, *via MCP* sits on the item and the note only, the three
  views sit in one strip under the head and swap only the middle, the
  timeline is oldest first with the notes in between and follows the
  filter, the people are counted from the items and lead to the timeline
  or the search, the remark stands under the item and is edited in a
  window, the conversation switch counts what the case lacks and *Thread
  +n* fetches it, a closed case draws no changing button, the export
  window is an overview with one button and no switch and opens the run
  window; the HTTP tests there and in `test_mcp_faelle.py` hold the
  routes and the MCP tools to the same rules.

## 9. Before you change the interface

1. Which door does it belong to? Build, search, Insights or settings.
2. Is there an existing component for it? Use that class.
3. Where is its state shown, and is it next to the thing it describes?
4. Is the explanation on an `(i)`?
5. Are the three language files updated, and does the i18n test pass?
6. Does the page still have one primary action per screen?
7. Run `pytest tests/test_app.py tests/test_i18n.py -q` and `ruff check .`.
