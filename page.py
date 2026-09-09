#!/usr/bin/env python3
"""
page.py – the interface, one embedded string.

Everything the browser gets is this single page: markup, styles and the
script, delivered by app.py's /-route with the language strings injected
(the /*__I18N__*/ placeholder) and the step metadata from the registry
(/*__STEPS__*/, a JSON block read the same way as the language strings).
One string on purpose: the bundle ships no template folder, and the page
tests read it as a string.
"""

PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Munimentum</title>
<!-- The archive box from packaging/icon/icon.svg, redrawn small. As a data
     URI so the bundle needs no extra file – otherwise every browser fetches
     a 404 on /favicon.ico. -->
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'%3E%3Crect width='1024' height='1024' rx='229' fill='%232f6fed'/%3E%3Crect x='196' y='330' width='632' height='158' rx='34' fill='%23fff'/%3E%3Crect x='246' y='500' width='532' height='300' rx='34' fill='%23fff' opacity='.93'/%3E%3Crect x='430' y='596' width='164' height='44' rx='22' fill='%232f6fed'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --muted:#5b6570; --line:#dfe3e8;
  --accent:#2f6fed; --ok:#1a7f4b; --warn:#a2650a; --err:#b3261e; --code:#f1f3f6;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#14171a; --card:#1c2024; --ink:#e8eaed; --muted:#9aa4ae; --line:#2c3238;
         --accent:#7aa2ff; --ok:#4cc38a; --warn:#e0a33a; --err:#f2837c; --code:#22272c; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:650}
header .marke{width:26px;height:26px;flex:0 0 auto;border-radius:6px;
  margin-right:-6px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto;align-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  border:1px solid var(--line);font-size:13px;cursor:pointer;background:transparent;color:inherit}
.pill:hover{border-color:var(--muted)}
/* "Quit" is not a state but an action – the gap separates it from the four
   indicators so nobody takes it for yet another status message. */
.pill-luecke{width:10px}
/* Explanation on demand instead of prose next to every button. The text lives
   in the title attribute – every browser shows it, every screen reader speaks
   it, and it needs no extra window that has to open and close again. */
h2.mit-info{display:flex;align-items:center;gap:8px}
.info{display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border-radius:50%;border:1px solid var(--line);
  color:var(--muted);font-size:11.5px;font-style:italic;font-weight:600;
  cursor:help;user-select:none;flex:0 0 auto}
.info:hover,.info:focus{color:var(--ink);border-color:var(--muted);outline:none}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.chk-sep{margin-top:10px;padding-top:8px;border-top:1px dashed var(--line)}
.chk-note{margin:4px 0 0;max-width:240px;color:var(--warn)}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.err{background:var(--err)}
nav{display:flex;gap:4px;padding:10px 20px 0;background:var(--card)}
nav button{border:0;background:transparent;color:var(--muted);padding:8px 14px;
  border-radius:8px 8px 0 0;font:inherit;cursor:pointer}
nav button.on{background:var(--bg);color:var(--ink);font-weight:600}
main{padding:20px;max-width:1080px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 4px}
.card p.sub{color:var(--muted);margin:0 0 14px;font-size:13px}
label.chk{display:flex;gap:8px;align-items:center;padding:4px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:6px 20px}
button.act{background:var(--accent);color:#fff;border:0;border-radius:8px;
  padding:9px 16px;font:inherit;font-weight:600;cursor:pointer}
button.act:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:transparent;border:1px solid var(--line);color:inherit;
  border-radius:8px;padding:9px 16px;font:inherit;cursor:pointer}
input[type=text],input[type=number],input[type=date],select,textarea{
  background:var(--bg);color:inherit;border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;font:inherit}
textarea{width:100%;min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
code,pre{background:var(--code);border-radius:6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
code{padding:2px 5px} pre{padding:12px;overflow-x:auto;margin:8px 0}
.muted{color:var(--muted)} .small{font-size:13px}
.ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)}
#log{background:#0d1013;color:#cbd3da;border-radius:10px;padding:12px;height:230px;
  overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap}
#log .l-head,.lauflog .l-head{color:#9ad0ff;font-weight:600}
#log .l-ok,.lauflog .l-ok{color:#7fdca4}
#log .l-warn,.lauflog .l-warn{color:#f0c674}
#log .l-err,.lauflog .l-err{color:#ff9c94}
/* The stored log of one run, inline in the runs table – the same dark
   console as the bar at the bottom. */
.lauflog{background:#0d1013;color:#cbd3da;border-radius:10px;padding:10px 12px;
  margin-top:8px;max-height:280px;overflow:auto;text-align:left;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  white-space:pre-wrap}
/* Hit row: two lines instead of four. Title on the left, origin and date on
   the right in their own columns – the dates stack up and you scan the list
   along its edge instead of reading it. The actions live in the menu: they
   differ per hit and would otherwise dominate the list. */
.hit{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;
  column-gap:14px;row-gap:3px;align-items:baseline;padding:10px 0;
  border-top:1px solid var(--line)}
.hit:first-child{border-top:0}
.dateizeile{display:flex;gap:10px;align-items:baseline;padding:8px 0;
  border-top:1px solid var(--line);cursor:default}
.dateizeile:first-child{border-top:0}
.dateizeile .muted{margin-left:auto;white-space:nowrap}
.urltab .zeile{display:flex;gap:8px;align-items:center;padding:5px 0;
  border-top:1px solid var(--line)}
.urltab .zeile:first-child{border-top:0}
.urltab .zeile.an{background:color-mix(in srgb, var(--accent) 8%, transparent)}
.urltab input[type=text]{flex:1;min-width:0}
.urltab:empty::after{content:attr(data-leer);color:var(--muted);font-size:12.5px}
.hit h3{grid-column:1;margin:0;font-size:14px;font-weight:600;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hit .wer{grid-column:2;color:var(--muted);font-size:12.5px;white-space:nowrap}
.hit .wann{grid-column:3;color:var(--muted);font-size:12.5px;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.hit .menuzelle{grid-column:4;position:relative;align-self:center}
.hit .prev{grid-column:1/-1;font-size:13.5px;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hit .verlauf{grid-column:1/-1}
@media (max-width:720px){
  .hit{grid-template-columns:minmax(0,1fr) auto}
  .hit .wer{grid-column:1;grid-row:2} .hit .wann{grid-column:2;grid-row:2}
  .hit .menuzelle{grid-column:2;grid-row:1} .hit .prev{grid-row:3}
}
.punkte-knopf{border:1px solid transparent;background:transparent;color:var(--muted);
  border-radius:7px;padding:2px 8px;font-size:16px;line-height:1.2;cursor:pointer}
.punkte-knopf:hover,.punkte-knopf[aria-expanded="true"]{border-color:var(--line);color:var(--ink)}
.menu{position:absolute;right:0;top:calc(100% + 4px);z-index:5;min-width:190px;
  background:var(--card);border:1px solid var(--line);border-radius:10px;
  box-shadow:0 6px 20px rgba(0,0,0,.14);padding:5px;display:flex;flex-direction:column}
.menu button{border:0;background:transparent;color:inherit;font:inherit;font-size:13.5px;
  text-align:left;padding:7px 10px;border-radius:7px;cursor:pointer}
.menu button:hover:not(:disabled){background:var(--code)}
.menu button:disabled{opacity:.4;cursor:not-allowed}
.menu hr{border:0;border-top:1px solid var(--line);margin:4px 2px}
/* Suggestions for the person field. The field is free-text input against a
   fixed inventory: whoever types a name the archive does not contain gets
   zero hits and cannot tell whether the person is missing or the name is
   misspelled. The list answers that before the search runs. */
.vorschlagfeld{position:relative;display:inline-block}
.vorschlagfeld #f-person{width:180px}
#personliste{left:0;right:auto;min-width:100%;max-width:320px}
#personliste button{display:flex;gap:10px;align-items:baseline;
  justify-content:space-between;width:100%}
#personliste button[aria-selected="true"]{background:var(--code)}
#personliste .zahl{color:var(--muted);font-size:12.5px;
  font-variant-numeric:tabular-nums;flex:0 0 auto}
#personliste .wer{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* The asterisk row is a pattern, not a name – set in the typeface patterns
   are read in. */
#personliste .wer.alle{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12.5px}
#personliste hr{border:0;border-top:1px solid var(--line);margin:4px 2px}
#personliste .leer{padding:7px 10px;font-size:13.5px;color:var(--muted)}
/* The search mode: an exclusive choice, the alternatives visible. */
.modi{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.modi button{border:0;background:transparent;color:var(--muted);font:inherit;
  font-size:13.5px;padding:7px 16px;cursor:pointer}
.modi button+button{border-left:1px solid var(--line)}
.modi button.on{background:var(--accent);color:#fff;font-weight:600}
.modi button:not(.on):not(:disabled):hover{color:var(--accent)}
.modi button:disabled{opacity:.4;cursor:not-allowed}
.modizeile{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
/* Match highlighting: subtle. The preview is set muted, the match gets the
   full text color and some weight – that lifts it out without making a long
   list look like a highlighter accident. */
mark{background:var(--code);color:var(--ink);font-weight:600;
  border-radius:3px;padding:0 3px}
.tag{display:inline-block;background:var(--code);border-radius:5px;padding:1px 6px;
  font-size:11.5px;color:var(--muted);margin-right:6px}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;
  align-items:center;justify-content:center;padding:20px;z-index:20}
#overlay.on{display:flex}
.modal{background:var(--card);border-radius:14px;max-width:660px;width:100%;
  max-height:88vh;overflow:auto;padding:24px}
.modal h2{margin:0 0 6px;font-size:18px}
/* All wizards share the same frame: title with a close cross at the top,
   exactly one primary and one secondary action at the bottom. */
.modal-kopf{display:flex;align-items:flex-start;gap:12px}
.modal-kopf h2{flex:1}
.modal-zu{flex:0 0 auto;border:0;background:transparent;color:var(--muted);
  font-size:22px;line-height:1;padding:0 4px;cursor:pointer;border-radius:6px}
.modal-zu:hover{color:var(--ink);background:var(--code)}
.modal-fuss{margin-top:16px;align-items:center}
/* The export list shows up to four hundred paths – it needs more width than
   a three-sentence wizard, and each group scrolls on its own so the third
   is not buried under the first. */
.modal.breit{max-width:860px}
.plangruppe{margin:10px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.plangruppe>summary{cursor:pointer;font-size:13.5px;font-weight:600}
.plangruppe>summary .dot{display:inline-block;margin-right:7px;vertical-align:middle}
.planliste{list-style:none;margin:8px 0 2px;padding:0;max-height:34vh;overflow:auto}
.planliste li{display:flex;gap:10px;align-items:baseline;padding:3px 0;font-size:13px;
  border-top:1px solid var(--line)}
.planliste li:first-child{border-top:0}
.planliste .pfad{flex:1;word-break:break-word}
.planliste .zahl{flex:0 0 auto;min-width:5em;text-align:right;color:var(--muted);
  font-variant-numeric:tabular-nums}
.planliste .regel{flex:0 0 auto;color:var(--muted);font-size:11.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
ol{padding-left:20px;margin:12px 0} ol li{margin-bottom:9px}
.banner{border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:13.5px;
  border:1px solid var(--line)}
.banner.warn{border-color:var(--warn)} .banner.err{border-color:var(--err)}
/* A field whose content the browser cannot read. On a date field that is
   otherwise invisible: it keeps showing what was typed but yields an empty
   value – and the search would silently run without that bound. */
input.fehler{border-color:var(--err)}
.hide{display:none!important}

/* ---- Calendar and address book (taken over from combined_search.py,
       adapted to the app's color variables so they work in dark mode too) ---- */
:root{
  --ev-ok:#2b6cb0; --ev-ok-bg:#eef4fb; --ev-warn:#c98a17; --ev-warn-bg:#fdf6e7;
  --ev-bad:#c0392b; --ev-bad-bg:#fbeceb; --ev-gone:#b6bbc2; --ev-gone-bg:#f2f3f5;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ev-ok:#7aa2ff; --ev-ok-bg:#1e2733; --ev-warn:#e0a33a; --ev-warn-bg:#2e2716;
    --ev-bad:#f2837c; --ev-bad-bg:#33201f; --ev-gone:#4a525a; --ev-gone-bg:#23272c;
  }
}
:root[data-theme="dark"]{
  --ev-ok:#7aa2ff; --ev-ok-bg:#1e2733; --ev-warn:#e0a33a; --ev-warn-bg:#2e2716;
  --ev-bad:#f2837c; --ev-bad-bg:#33201f; --ev-gone:#4a525a; --ev-gone-bg:#23272c;
}
.calbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.chip{padding:6px 13px;border:1px solid var(--line);border-radius:8px;background:transparent;
  color:var(--muted);font-size:13.5px;cursor:pointer}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
#kalTitle{font-weight:650;margin-left:4px}
.legend{display:flex;gap:12px;margin-left:auto;font-size:12px;color:var(--muted);align-items:center;flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.grid{display:grid;gap:8px}
/* minmax(0,…): otherwise long event titles blow up the column width */
.wk,.mo{grid-template-columns:repeat(7,minmax(0,1fr))}
.mo{gap:6px}
.dow{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;padding:0 2px}
.day{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:110px;min-width:0}
.day.today{border-color:var(--accent);box-shadow:0 0 0 2px rgba(122,162,255,.18)}
.day.out{opacity:.5}
.dnum{font-size:12px;color:var(--muted);margin-bottom:5px;display:flex;gap:5px;align-items:baseline}
.dnum b{font-size:14px;color:var(--ink)}
.dnum .wd{display:none}          /* the weekday is already in the column header */
.ev{display:block;font-size:12px;line-height:1.35;margin:3px 0;padding:4px 6px;border-radius:6px;
  text-decoration:none;border-left:3px solid var(--ev-ok);background:var(--ev-ok-bg);color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev:hover{white-space:normal}
.ev .evt{color:var(--muted);font-variant-numeric:tabular-nums}
.ev.tentative{border-left-color:var(--ev-warn);background:var(--ev-warn-bg);border-left-style:dashed}
.ev.cancelled{border-left-color:var(--ev-bad);background:var(--ev-bad-bg);text-decoration:line-through;opacity:.75}
/* reconstructed from mails only: dashed border instead of a bar */
.ev.deleted{border:1px dashed var(--ev-bad);border-left-width:3px;background:var(--ev-bad-bg);text-decoration:line-through;opacity:.85}
.ev.gone{border:1px dashed var(--ev-gone);border-left-width:3px;background:var(--ev-gone-bg);color:var(--muted)}
.mo .day{min-height:96px}
@media(max-width:820px){.wk,.mo{grid-template-columns:minmax(0,1fr)}.dowrow{display:none}
  .day{min-height:0}.dnum .wd{display:inline}}
.rbnote{color:var(--muted);font-size:12.5px;margin:0 0 10px}
.rbcount{color:var(--muted);font-size:12px;margin-left:auto}
.rbmonth{margin:16px 0 6px;font-size:13px;font-weight:700;color:var(--muted);
  border-bottom:1px solid var(--line);padding-bottom:3px}
.rbrow{display:flex;gap:10px;align-items:baseline;background:var(--card);border:1px solid var(--line);
  border-radius:9px;padding:8px 11px;margin:5px 0;text-decoration:none;color:var(--ink)}
.rbrow:hover{border-color:var(--accent)}
.rbrow.deleted{border-left:3px solid var(--ev-bad)}
.rbrow.gone{border-left:3px solid var(--ev-gone)}
.rbdate{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:158px}
.rbstate{font-size:11px;padding:2px 8px;border-radius:6px;font-weight:600;white-space:nowrap}
.rbrow.deleted .rbstate{background:var(--ev-bad-bg);color:var(--ev-bad)}
.rbrow.gone .rbstate{background:var(--ev-gone-bg);color:var(--muted)}
.rbtitle{font-weight:600;overflow-wrap:anywhere;min-width:0;flex:1}
.rbrow.deleted .rbtitle{text-decoration:line-through}
.rbwho{color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
@media(max-width:820px){.rbrow{flex-wrap:wrap;gap:4px 9px}.rbwho{max-width:none}}
.letter{margin:18px 0 6px;font-size:13px;font-weight:700;color:var(--muted);
  border-bottom:1px solid var(--line);padding-bottom:3px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.card2{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
.cname{font-weight:600;overflow-wrap:anywhere}
.cname a{color:var(--ink);text-decoration:none}
.cname a:hover{color:var(--accent);text-decoration:underline}
.crole{font-size:12.5px;color:var(--muted);margin-bottom:5px;overflow-wrap:anywhere}
.cline{font-size:13px;overflow-wrap:anywhere}
.cline a{color:var(--accent);text-decoration:none}
.cline span{color:var(--muted);margin-right:5px}
.hint{color:var(--muted)}

/* Progress: step by step, and within one step as precise as the script
   knows. Where there is no total, the bar keeps moving striped instead of
   inventing a percentage. */
.fortschritt{margin-top:14px}
.balken{height:8px;background:var(--code);border-radius:99px;overflow:hidden}
.balken>div{height:100%;background:var(--accent);border-radius:99px;
  transition:width .3s ease;width:0}
.balken.unbekannt>div{width:35%;background:linear-gradient(90deg,
  var(--code) 0%,var(--accent) 50%,var(--code) 100%);animation:wandern 1.6s linear infinite}
@keyframes wandern{from{transform:translateX(-100%)}to{transform:translateX(340%)}}

/* Permissions in the token wizard: an unobtrusive line as long as they are
   not the problem. */
details.rechte{margin:12px 0;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
details.rechte summary{cursor:pointer;font-size:13px;color:var(--muted)}
details.rechte[open] summary{margin-bottom:4px;color:var(--ink)}
details.rechte p{margin:6px 0}

/* The choice between the two sign-in paths: two equal cards, so neither
   looks like a footnote of the other. */
.wahlreihe{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.wahl{flex:1 1 240px;display:flex;gap:9px;align-items:flex-start;cursor:pointer;
  border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.wahl.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.wahl input{margin-top:3px}
.wahl span{display:flex;flex-direction:column;gap:2px}
button.mini,a.mini{border:1px solid var(--line);background:transparent;color:inherit;
  border-radius:8px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
/* A link with the same job should look the same – as plain link-colored text
   it would sit next to the buttons like a foreign object. */
a.mini{display:inline-block;text-decoration:none;line-height:1.5}
button.mini:hover,a.mini:hover{border-color:var(--accent);color:var(--accent)}
/* Copy button in the corner of the box – visible without covering the content. */
.mitkopie{position:relative}
.mitkopie pre{padding-right:96px}
button.kopie{position:absolute;top:8px;right:8px;background:var(--card)}
/* Thread history below a hit: narrow and quiet so it does not overwhelm
   the hit list. */
/* Deleted items are the exception and may stand out – but only as far as
   keeps the hit list calm. */
.tag.weg{border-color:var(--warn);color:var(--warn)}
.tag.herkunft{margin-left:8px;font-weight:400}
/* Analytics: key figures as a calm grid, not as a dashboard cockpit. */
.kpis{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
/* Search field and button belong together and fill the row – the picture
   everyone knows from other programs. Everything else sits below. */
.suchzeile{display:flex;gap:8px}
.suchzeile input{flex:1;min-width:200px;font-size:15px;padding:9px 12px}
.suchzeile button{flex:0 0 auto;padding:9px 20px}
.feld{display:flex;align-items:center;gap:6px;color:var(--muted)}
.kpi{border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi.klickbar{cursor:pointer}
.kpi.klickbar:hover,.kpi.klickbar:focus{border-color:var(--accent);outline:none}
.kpi-titel{display:flex;align-items:center;gap:6px}
.kpi-wert{font-size:26px;font-weight:650;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi-titel{font-size:13.5px;margin-top:2px}
.kpi-hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4}
.kpi-fuss{grid-column:1/-1}
.anatab{width:100%;border-collapse:collapse;margin-top:12px;font-size:13.5px}
.anatab th{text-align:left;font-weight:600;border-bottom:1px solid var(--line);padding:6px 8px}
.anatab td{padding:5px 8px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums}
.anatab td:not(:first-child), .anatab th:not(:first-child){text-align:right}
.anatab td.fehlt{color:var(--warn);font-weight:600}
.warnzeile{color:var(--warn);font-weight:600;margin:0}
.okzeile{color:var(--ok);font-weight:600;margin:0}
.card2 button.mini{margin-top:8px}
.verlauf{margin-top:8px}
.verlaufliste{border-left:2px solid var(--line);padding-left:12px;margin-top:6px}
.verlaufliste p{margin:0 0 6px}
.vzeile{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
  font-size:13.5px;padding:3px 0}
.vdatum{color:var(--muted);font-variant-numeric:tabular-nums;flex:0 0 auto}
.vwer{color:var(--muted);flex:0 0 auto;min-width:120px}
.geraetecode{border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0;
  text-align:center}
.geraetecode p{margin:0 0 8px}
.code-gross{display:inline-block;font-size:26px;letter-spacing:.14em;font-weight:700;
  background:var(--code);border-radius:8px;padding:8px 16px}

/* Settings: one row per setting, built the same everywhere – label on the
   left with an (i), control on the right. The explanations live in the (i)
   instead of prose below; that makes the page scannable rather than
   something to read. */
.feldzeile{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;
  align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.gruppe>.feldzeile:first-of-type{border-top:0}
.feldzeile .bez{display:flex;align-items:center;gap:7px;font-size:14px}
.feldzeile input[type=text]{width:240px}
.feldzeile select{min-width:200px}
.feldzeile input[type=number]{width:96px;text-align:right;font-variant-numeric:tabular-nums}
.feldzeile.breit{grid-template-columns:1fr;gap:6px}
.feldzeile.breit textarea{min-height:76px}
.gruppe{margin-top:20px}
.card>h2+.gruppe{margin-top:10px}
/* Action rows: the status text at the left, the buttons at the right –
   the same shape under every group, no matter which card. */
.aktionen{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:flex-end;margin-top:10px}
.aktionen>.small:first-child{margin-right:auto}
.aktionen:has(>:only-child:empty){display:none}
.gruppe>h3{font-size:12px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--muted);margin:0 0 4px;display:flex;align-items:center;gap:7px}
.aus{opacity:.42;pointer-events:none}
/* Every boolean in the settings is this switch – the class is for the
   one outside a settings row (the search filter). */
.kipp,.feldzeile input[type=checkbox]{appearance:none;width:40px;height:23px;border-radius:12px;background:var(--line);
  position:relative;cursor:pointer;transition:background .15s;flex:0 0 auto}
.kipp:checked,.feldzeile input[type=checkbox]:checked{background:var(--accent)}
.kipp::after,.feldzeile input[type=checkbox]::after{content:"";position:absolute;top:3px;left:3px;width:17px;height:17px;
  border-radius:50%;background:#fff;transition:transform .15s}
.kipp:checked::after,.feldzeile input[type=checkbox]:checked::after{transform:translateX(17px)}
.kipp:focus-visible,.feldzeile input[type=checkbox]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* "Deleted" sits as a toggle in the filter row: it is not a value picked
   from a list but a state – on or off. Everything level with the selects
   next to it, the explanation behind it instead of below. */
.gonefeld{display:inline-flex;align-items:center;gap:8px;
  border:1px solid var(--line);border-radius:8px;padding:5px 10px}
.gonefeld label{color:var(--muted);cursor:pointer;white-space:nowrap}
.gonefeld input:checked ~ label,.gonefeld:hover label{color:var(--ink)}
/* Small status indicator next to a field: the dot carries the color, the
   word next to it the information. Both together, because color alone helps
   nobody who cannot tell the colors apart. */
.feldmitstand{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap}
.stand{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;
  color:var(--muted);white-space:nowrap}
.stand .dot.ok{background:var(--ok)} .stand .dot.warn{background:var(--warn)}
.stand .dot.err{background:var(--err)}
.folgen{font-size:12.5px;color:var(--muted);margin:10px 0 0}
.folgen:empty{display:none}
/* The grayed-out part continues the list below the switch – no group gap,
   the divider stays. */
#ollama-kinder>.gruppe:first-child{margin-top:0}
#ollama-kinder>.gruppe:first-child>.feldzeile:first-of-type{border-top:1px solid var(--line)}
.speichern{position:sticky;bottom:0;background:var(--bg);padding:14px 0;
  border-top:1px solid var(--line);display:flex;gap:12px;align-items:center;z-index:5}

/* Charts. Two series carry color – Teams and mail –, everything else is
   plain quantity and gets one tone. The two values are checked against the
   app's real surfaces (contrast, color blindness); "other" is deliberately
   not a third color but the catch-all column in gray. */
:root{ --serie-a:#2a78d6; --serie-b:#eb6834; --serie-c:#9aa4ae; }
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){ --serie-a:#3987e5; --serie-b:#d95926; --serie-c:#5b6570; }
}
:root[data-theme="dark"]{ --serie-a:#3987e5; --serie-b:#d95926; --serie-c:#5b6570; }
.dia{width:100%;height:auto;display:block;overflow:visible}
.dia rect,.dia path{shape-rendering:crispEdges}
.dia .achse{stroke:var(--line);stroke-width:1}
.dia .tick{fill:var(--muted);font-size:10px}
.dia .linie{fill:none;stroke:var(--serie-a);stroke-width:2;shape-rendering:geometricPrecision}
.legende{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);
  margin:8px 0 2px}
.legende span{display:inline-flex;align-items:center;gap:6px}
.legende i{width:10px;height:10px;border-radius:2px;display:inline-block}
/* Horizontal bars for rankings: label, bar, number – the number
   right-aligned with tabular figures so the column holds still.

   ONE grid for the whole list, not one per row: otherwise each row follows
   its own label, the bars start at nine different places and can no longer
   be compared – which is what they are for. The name column is as wide as
   its longest entry, but at most 420px: a file path must not crowd out the
   bar. */
.rangliste{display:grid;grid-template-columns:minmax(90px,max-content) 1fr auto;
  gap:8px 10px;align-items:center;font-size:13px;margin:2px 0}
.rangliste .bal{background:var(--code);border-radius:4px;height:9px;position:relative}
.rangliste .bal i{position:absolute;inset:0 auto 0 0;background:var(--serie-a);
  border-radius:4px;display:block}
.rangliste .zahl{color:var(--muted);font-variant-numeric:tabular-nums;
  font-size:12.5px;text-align:right}
.rangliste .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  max-width:420px}
.dia-titel{font-size:13px;font-weight:600;margin:18px 0 2px}
.dia-sub{font-size:12.5px;color:var(--muted);margin:0 0 8px}

/* Log bar at the bottom */
#protokoll{position:fixed;left:0;right:0;bottom:0;background:var(--card);
  border-top:1px solid var(--line);z-index:15;box-shadow:0 -2px 12px rgba(0,0,0,.10)}
#protokoll .pkopf{display:flex;gap:10px;align-items:center;padding:8px 20px;
  cursor:pointer;user-select:none}
#protokoll .pkopf .pfeil{color:var(--muted);transition:transform .2s}
#protokoll.zu .pkopf .pfeil{transform:rotate(180deg)}
#protokoll .pkopf #log-letzte{overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1}
/* The buttons on the right: small and quiet, so the row still reads as a
   caption and not as a toolbar. */
#protokoll .pkopf button.mini{flex:0 0 auto;padding:3px 10px;font-size:12px}
#protokoll.zu #log{display:none}
#protokoll #log{margin:0 12px 12px;height:var(--loghoehe,220px)}
/* The drag handle: a narrow strip along the top edge. */
#protokoll .pgriff{position:absolute;left:0;right:0;top:-4px;height:9px;
  cursor:ns-resize;touch-action:none}
#protokoll.zu .pgriff,#protokoll.hide .pgriff{display:none}
main{padding-bottom:60px}   /* until the script sets the real log height */

/* Answer box. Deliberately unlike a hit card: what stands here was written
   by no human but summarized by a model from the hits below. Colored bar on
   the left, its own header, footnotes. */
.answer{background:var(--card);border:1px solid var(--line);
  border-left:4px solid var(--accent);border-radius:12px;padding:14px 18px;margin-bottom:16px}
.answer .ahead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font-size:12px;color:var(--muted);margin-bottom:8px}
.answer .ahead .tag{background:var(--accent);color:#fff;border-radius:5px;
  padding:1px 8px;font-weight:700;letter-spacing:.04em}
.answer .atext{white-space:pre-wrap;overflow-wrap:anywhere}
.answer .atext a{color:var(--accent);text-decoration:none;font-weight:600}
.answer .afoot{font-size:12px;color:var(--muted);margin-top:10px;
  border-top:1px solid var(--line);padding-top:8px}
.answer.err{border-left-color:var(--err)}
.blink::after{content:"▍";animation:blink 1s steps(2,start) infinite}
@keyframes blink{to{visibility:hidden}}
.hit.zitiert{background:var(--code);border-radius:8px;padding-left:10px;
  margin-left:-10px;box-shadow:inset 3px 0 0 var(--accent)}
.hit .fussnote{color:var(--accent);font-weight:700;margin-right:6px}
</style>
</head>
<body>
<header>
  <!-- The same small redraw of packaging/icon/icon.svg as the favicon –
       inline, so the bundled app needs no asset route for it. -->
  <svg class="marke" viewBox="0 0 1024 1024" aria-hidden="true"><rect width="1024" height="1024" rx="229" fill="#2f6fed"/><rect x="196" y="330" width="632" height="158" rx="34" fill="#fff"/><rect x="246" y="500" width="532" height="300" rx="34" fill="#fff" opacity=".93"/><rect x="430" y="596" width="164" height="44" rx="22" fill="#2f6fed"/></svg>
  <h1 data-i18n="app.title">Munimentum</h1>
  <!-- The pills say what the state means for the user; the technical term
       (token, Ollama, chunks, MCP) sits in the tooltip, so whoever needs it
       can find it without making everyone who doesn't know it read it. -->
  <div class="pills" id="pills">
    <button class="pill" id="pill-token" onclick="openWizard('token')"><span class="dot" id="p-token"></span><span id="p-token-t">Zugang</span></button>
    <button class="pill" id="pill-ollama" onclick="ollamaKachel()"><span class="dot" id="p-ollama"></span><span id="p-ollama-t">KI-Suche</span></button>
    <button class="pill" id="pill-mcp" onclick="zeigeEinstellung('mcp-karte')"><span class="dot" id="p-mcp"></span><span id="p-mcp-t">Claude</span></button>
    <span class="pill-luecke"></span>
    <button class="pill" onclick="beenden()" id="btn-quit" data-i18n="app.quit"
            data-i18n-title="app.quit.tip"
            style="border-color:var(--err);color:var(--err)">Beenden</button>
  </div>
</header>

<nav>
  <button data-tab="export" class="on" onclick="tab('export')" data-i18n="nav.export">Daten exportieren</button>
  <button data-tab="suche" onclick="tab('suche')" data-i18n="nav.search">Daten durchsuchen</button>
  <button data-tab="analytics" onclick="tab('analytics')" data-i18n="nav.analytics">Analytics</button>
  <button data-tab="einstellungen" onclick="tab('einstellungen')" data-i18n="nav.settings">Einstellungen</button>
</nav>

<main>
<section id="tab-export">
  <div class="banner hide" id="update-banner" style="margin-bottom:16px"></div>
  <div class="card">
    <h2 class="mit-info" data-i18n="export.what">Was soll exportiert werden?
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="export.what.sub" role="img" aria-label="Info">i</span></h2>
    <div class="row" style="gap:36px;align-items:flex-start">
      <div>
        <strong class="small" data-i18n="export.outlook">Outlook</strong>
        <div id="cat-outlook"></div>
      </div>
      <div>
        <strong class="small" data-i18n="export.teams">Teams</strong>
        <div id="cat-teams"></div>
      </div>
      <div>
        <strong class="small" data-i18n="export.onedrive">OneDrive</strong>
        <label class="chk"><input type="checkbox" id="c-onedrive_enabled" onchange="saveCats()">
          <span data-i18n="export.cat.files">OneDrive-Dateien</span></label>
      </div>
      <div>
        <strong class="small" data-i18n="export.sharepoint">SharePoint</strong>
        <label class="chk"><input type="checkbox" id="c-sharepoint_enabled" onchange="saveCats()">
          <span data-i18n="export.cat.sharepoint">SharePoint-Bibliotheken</span></label>
        <label class="chk"><input type="checkbox" id="c-sharepoint_pages_enabled" onchange="saveCats()">
          <span data-i18n="export.cat.pages">Site-Seiten</span></label>
        <p class="small muted" id="sp-export-note" style="max-width:240px;margin:4px 0 0"></p>
      </div>
      <div>
        <strong class="small" data-i18n="export.planner">Planner</strong>
        <label class="chk"><input type="checkbox" id="c-planner_enabled" onchange="saveCats()">
          <span data-i18n="export.cat.planner">Boards</span></label>
      </div>
    </div>
    <div class="row" style="margin-top:14px">
      <button class="act" id="btn-run" onclick="runExport()" data-i18n="export.start">Export starten</button>
      <button class="ghost hide" id="btn-cancel" onclick="merke('flow.cancel');post('/api/cancel')" data-i18n="export.cancel">Abbrechen</button>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="export.start.hint"
            role="img" aria-label="Info">i</span>
    </div>

    <div class="fortschritt hide" id="fortschritt">
      <div class="balken"><div id="balken-fuell"></div></div>
      <p class="small muted" id="fortschritt-text"></p>
    </div>

  </div>

</section>

<section id="tab-suche" class="hide">
  <div class="calbar" id="sichten" style="margin-bottom:14px">
    <span class="chip on" data-sicht="treffer" onclick="sicht('treffer')" data-i18n="view.hits">Treffer</span>
    <span class="chip" data-sicht="kalender" onclick="sicht('kalender')" data-i18n="nav.calendar">Kalender</span>
    <span class="chip" data-sicht="adressbuch" onclick="sicht('adressbuch')" data-i18n="nav.book">Adressbuch</span>
    <span class="chip" data-sicht="dateien" onclick="sicht('dateien')" data-i18n="view.files">Dateien</span>
  </div>

  <div id="sicht-treffer">
  <div class="card">
    <div class="suchzeile">
      <input type="search" id="q" data-i18n-ph="search.query.ph"
             placeholder="Suchbegriff oder Frage"
             onkeydown="if(event.key==='Enter'){sofortSuchen();}">
      <button class="act" onclick="sofortSuchen()" data-i18n="search.go">Suchen</button>
    </div>
    <!-- The search mode sits right below the field because it determines
         what makes sense to type there – the placeholder changes with it.
         No separate explanation line; it would only be in the way. -->
    <div class="modizeile">
      <div class="modi" role="group" aria-label="Suchart" data-i18n-title="search.mode">
        <button id="m-text" class="on" onclick="suchmodus('text')"
                data-i18n="search.mode.text">Textsuche</button>
        <button id="m-aehnlich" onclick="suchmodus('aehnlich')"
                data-i18n="search.mode.aehnlich">Ähnliche Suche</button>
        <button id="m-ki" onclick="suchmodus('ki')"
                data-i18n="search.mode.ki">KI-Zusammenfassung</button>
      </div>
      <span class="small muted hide" id="modus-fehlt"
            data-i18n="search.mode.needs">Braucht Ollama.</span>
    </div>
    <div class="row" style="margin-top:10px;gap:10px">
      <button class="mini" id="filter-auf" aria-expanded="false"
              onclick="filterUmschalten()" data-i18n="search.filter">Filter</button>
      <button class="mini hide" id="filter-weg" onclick="filterLeeren()"
              data-i18n="search.filter.clear">Zurücksetzen</button>
    </div>
    <div class="row hide" id="filter" style="margin-top:10px">
      <span class="vorschlagfeld">
        <input type="text" id="f-person" data-i18n-ph="search.person.ph" placeholder="Person"
               role="combobox" aria-expanded="false" aria-autocomplete="list"
               aria-controls="personliste" autocomplete="off"
               oninput="personVorschlagen()" onkeydown="personTaste(event)"
               onchange="zeigeFilterstand()">
        <div class="menu hide" id="personliste" role="listbox"></div>
      </span>
      <select id="f-source" onchange="ladeOrdner();zeigeFilterstand()">
        <option value="all" data-i18n="search.source.all">Alle Quellen</option><option value="teams" data-i18n="search.source.teams">Teams</option>
        <option value="outlook" data-i18n="search.source.outlook">Mail</option>
        <option value="kalender" data-i18n="search.source.kalender">Kalender</option>
        <option value="kontakte" data-i18n="search.source.kontakte">Kontakte</option>
        <option value="onedrive" data-i18n="search.source.onedrive">OneDrive</option>
        <option value="sharepoint" data-i18n="search.source.sharepoint">SharePoint</option>
        <option value="pages" data-i18n="search.source.pages">Site-Seiten</option>
        <option value="planner" data-i18n="search.source.planner">Planner</option>
      </select>
      <label class="small feld"><span data-i18n="search.from">von</span>
        <input type="date" id="f-from" onchange="zeigeFilterstand()"></label>
      <label class="small feld"><span data-i18n="search.to">bis</span>
        <input type="date" id="f-to" onchange="zeigeFilterstand()"></label>
      <select id="f-typ" onchange="zeigeFilterstand()" style="max-width:170px">
        <option value="" data-i18n="search.type.all">Alle Dateitypen</option>
      </select>
      <select id="f-folder" onchange="zeigeFilterstand()" style="max-width:260px">
        <option value="" data-i18n="search.folder.all">Alle Ordner</option>
      </select>
      <span class="gonefeld" id="gone-feld">
        <label class="feld small" for="f-gone" data-i18n="view.gone">Gelöschtes</label>
        <input type="checkbox" class="kipp" id="f-gone" onchange="zeigeFilterstand()">
        <span class="info" tabindex="0" aria-label="i" data-i18n-title="search.gone.note">i</span>
      </span>
    </div>
  </div>
  <div class="answer hide" id="ai-box"></div>
  <div class="card">
    <!-- In the AI variant the answer sits on top; the hits it rests on are
         one click away instead of gone. -->
    <div class="row hide" id="ki-klappe" style="margin-bottom:12px">
      <button class="mini" id="ki-klappknopf" onclick="klappeTreffer()"></button>
    </div>
    <div id="results" class="muted small" data-i18n="search.none.yet">Noch keine Suche.</div>
    <div class="row" id="pager" style="margin-top:12px"></div></div>
  </div>

  <div id="sicht-dateien" class="hide">
    <div class="row" style="margin:10px 0 6px;align-items:center">
      <p class="small muted" id="dateien-pfad" style="flex:1;margin:0"></p>
      <button class="mini hide" id="dateien-suchen" onclick="dateienSuchen()"
              data-i18n="files.search.here">Hier suchen</button>
    </div>
    <div id="dateien-liste"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
  </div>

  <div id="sicht-kalender" class="hide">
  <div class="card">
    <div class="calbar">
      <span class="chip on" data-mode="week" data-i18n="cal.week">Woche</span>
      <span class="chip" data-mode="month" data-i18n="cal.month">Monat</span>
      <span class="chip" data-mode="rebuilt" data-i18n="cal.rebuilt">Rekonstruiert</span>
      <span id="kalNav">
        <button class="ghost" id="kalPrev" style="padding:6px 12px">‹</button>
        <button class="ghost" id="kalToday" style="padding:6px 12px" data-i18n="cal.today">Heute</button>
        <button class="ghost" id="kalNext" style="padding:6px 12px">›</button>
      </span>
      <span id="kalTitle"></span>
      <span class="legend" id="kalLegend">
        <span><i style="background:var(--ev-ok)"></i><span data-i18n="cal.legend.confirmed">Bestätigt</span></span>
        <span><i style="background:var(--ev-warn)"></i><span data-i18n="cal.legend.tentative">Vorläufig</span></span>
        <span><i style="background:var(--ev-bad)"></i><span data-i18n="cal.legend.cancelled">Abgesagt</span></span>
        <span><i style="background:var(--ev-gone)"></i><span data-i18n="cal.legend.rebuilt">Rekonstruiert</span></span>
      </span>
    </div>
    <p class="small muted" id="kalStats"></p>
    <div id="kalBox"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
  </div>
  </div>

  <div id="sicht-adressbuch" class="hide">
  <div class="card">
    <div class="calbar">
      <span class="chip on" data-book="all" data-i18n="book.f.all">Alle</span>
      <span class="chip" data-book="contacts" data-i18n="book.f.contacts">Aus Kontakten</span>
      <span class="chip" data-book="comm" data-i18n="book.f.comm">Aus Kommunikation</span>
      <input type="text" id="kbQ" data-i18n-ph="book.search.ph" placeholder="Name, Firma, Mail oder Telefon…" style="flex:1;min-width:220px">
      <span class="small muted" id="kbStats"></span>
    </div>
    <p class="small muted" style="margin:8px 0 0" data-i18n="book.f.note">Kontakte stammen aus dem Outlook-Adressbuch, Kommunikation aus Absendern und Empfängern.</p>
  </div>
  <div class="card"><div id="kbBox"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div></div>
  </div>
</section>




<section id="tab-analytics" class="hide">
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 data-i18n="ana.title" style="margin:0">Was im Archiv steckt</h2>
      <span class="row" style="gap:10px"><span class="small muted" id="ana-stand"></span>
      <button class="mini" onclick="ladeAnalytics(true)" data-i18n="ana.reload">Aktualisieren</button></span>
    </div>
    <p class="sub" data-i18n="ana.sub">Beim Indexlauf gerechnet – ohne Microsoft zu fragen.</p>
    <div class="dia-titel" data-i18n="ana.komm.title">Kommunikation</div>
    <div id="ana-kpi" class="kpis"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
    <div class="dia-titel" data-i18n="ana.dateien.title">Dateien &amp; Platz</div>
    <div id="ana-kpi-dateien" class="kpis"></div>
    <p class="small muted" style="margin-top:10px" id="export-state"></p>
  </div>

  <div class="card">
    <h2 data-i18n="ana.verlauf.titel">Verlauf</h2>
    <div id="ana-dia"></div>
  </div>

  <div class="card">
    <h2 data-i18n="ana.health.title">Gesundheit</h2>
    <div class="dia-titel" data-i18n="ana.runs.title">Läufe</div>
    <p class="dia-sub" data-i18n="ana.runs.sub">Jeder Lauf der App, mit Dauer und Ergebnis je Schritt.</p>
    <div id="ana-runs"><p class="hint" data-i18n="cal.loading">Wird geladen…</p></div>
    <div class="dia-titel" data-i18n="ana.check.title">Vollständigkeit</div>
    <p class="dia-sub" data-i18n="ana.check.sub">Vergleicht, was Microsoft je Ordner zählt, mit dem, was hier liegt.</p>
    <div class="row">
      <button class="act" id="ana-check" onclick="pruefeVollstaendigkeit()" data-i18n="ana.check.run">Jetzt prüfen</button>
      <span class="small muted" id="ana-check-state"></span>
    </div>
    <div id="ana-checks"></div>
  </div>
</section>

<section id="tab-einstellungen" class="hide">
  <div class="card">
    <h2 class="mit-info"><span data-i18n="settings.export.title">Export</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.export.i">i</span></h2>

    <div class="gruppe"><h3 data-i18n="settings.teams.title">Teams</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.embed_images"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.embed_images.i">i</span></span><input type="checkbox" id="c-embed_images"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.cache_images"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.cache_images.i">i</span></span><input type="checkbox" id="c-cache_images"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.refresh_channels"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.refresh_channels.i">i</span></span><input type="checkbox" id="c-refresh_channels"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.skip_empty_chats"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.skip_empty_chats.i">i</span></span><input type="checkbox" id="c-skip_empty_chats"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.cadence"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.cadence.i">i</span></span><select id="c-cadence-teams"><option value="always" data-i18n="cadence.always"></option><option value="daily" data-i18n="cadence.daily"></option><option value="weekly" data-i18n="cadence.weekly"></option><option value="monthly" data-i18n="cadence.monthly"></option></select></div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.outlook.title">Outlook</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.include_hidden"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.include_hidden.i">i</span></span><input type="checkbox" id="c-include_hidden"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.calendar_reconstruct"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.calendar_reconstruct.i">i</span></span><input type="checkbox" id="c-calendar_reconstruct"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="folders.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="folders.rules.i">i</span></span><span class="small muted" id="folders-state"></span></div>
      <div class="feldzeile breit">
        <textarea id="c-folder_rules"
          placeholder="- E-Mail/Archiv/**&#10;+ E-Mail/Archiv/Wichtig/**"></textarea>
      </div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.skip_folders.sub"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.skip_folders.i">i</span></span><span class="small muted"></span></div>
      <div class="feldzeile breit"><textarea id="c-skip_folders"></textarea></div>
      <div class="aktionen">
        <span class="small muted" id="folders-msg"></span>
        <button class="mini" onclick="gleicheOrdnerAb()" data-i18n="folders.sync">Ordnerstruktur abgleichen</button>
        <button class="mini" onclick="zeigeExportliste()" data-i18n="plan.open">Exportliste anzeigen</button>
        <button class="mini" onclick="ordnerZuruecksetzen()" data-i18n="settings.skip_folders.reset">Auf Vorgabe zurücksetzen</button>
      </div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.calendars.title">Kalender</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.calendars.rules"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.calendars.rules.i">i</span></span><span class="small muted" id="cal-state"></span></div>
      <div class="feldzeile breit">
        <textarea id="c-calendar_rules"
          placeholder="- kalender/**&#10;+ kalender/Privat"></textarea>
      </div>
      <div class="aktionen">
        <span class="small muted" id="cal-msg"></span>
        <button class="mini" onclick="gleicheOrdnerAb('calendar')" data-i18n="settings.calendars.sync">Kalenderliste abgleichen</button>
        <button class="mini" onclick="zeigeExportliste('calendar')" data-i18n="plan.open">Exportliste anzeigen</button>
      </div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.onedrive.title">OneDrive</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.onedrive.rules.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.onedrive.rules.i">i</span></span><span class="small muted" id="od-folders-state"></span></div>
      <div class="feldzeile breit">
        <textarea id="c-onedrive_rules"
          placeholder="- Dateien/Fotos/**&#10;+ Dateien/Fotos/Wichtig/**"></textarea>
      </div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.onedrive.maxmb"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.onedrive.maxmb.i">i</span></span><span><input type="number" id="c-onedrive_max_mb" min="0" step="10"> <span class="muted small">MB</span></span></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.cadence"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.cadence.i">i</span></span><select id="c-cadence-onedrive"><option value="always" data-i18n="cadence.always"></option><option value="daily" data-i18n="cadence.daily"></option><option value="weekly" data-i18n="cadence.weekly"></option><option value="monthly" data-i18n="cadence.monthly"></option></select></div>
      <div class="aktionen">
        <span class="small muted" id="od-folders-msg"></span>
        <button class="mini" onclick="gleicheOrdnerAb('onedrive')" data-i18n="folders.sync">Ordnerstruktur abgleichen</button>
        <button class="mini" onclick="zeigeExportliste('onedrive')" data-i18n="plan.open">Exportliste anzeigen</button>
      </div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.sharepoint.title">SharePoint</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.sharepoint.urls.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.sharepoint.urls.i">i</span></span><span>
        <button class="mini" onclick="urlZeile('sp-urls', '')" title="+">+</button>
        <button class="mini" onclick="urlZeileWeg('sp-urls')" title="&minus;">&minus;</button></span></div>
      <div id="sp-urls" class="urltab" data-praefix="sharepoint-url"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.sharepoint.include"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.sharepoint.include.i">i</span></span><input type="text" id="c-sharepoint_types_include" placeholder="pdf, docx, xlsx"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.sharepoint.exclude"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.sharepoint.exclude.i">i</span></span><input type="text" id="c-sharepoint_types_exclude" placeholder="mp4, iso"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.sharepoint.maxmb"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.sharepoint.maxmb.i">i</span></span><span><input type="number" id="c-sharepoint_max_mb" min="0" step="10"> <span class="muted small">MB</span></span></div>
      <div class="aktionen">
        <span class="small muted" id="sp-msg"></span>
        <button class="mini" onclick="gleicheOrdnerAb('sharepoint')" data-i18n="folders.sync">Ordnerstruktur abgleichen</button>
        <button class="mini" onclick="sharepointVorschau()" data-i18n="sharepoint.preview">Größen-Vorschau</button>
        <button class="mini" onclick="zeigeExportliste('sharepoint')" data-i18n="plan.open">Exportliste anzeigen</button>
        <button class="mini" onclick="zeigeSharepointTypen()" data-i18n="sharepoint.types">Dateitypen anzeigen</button>
      </div>
      <div class="small muted" id="sp-typen" style="margin-top:6px"></div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.pages.title">SharePoint-Seiten</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.sharepoint.pages.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.sharepoint.pages.i">i</span></span><span>
        <button class="mini" onclick="urlZeile('pg-urls', '')" title="+">+</button>
        <button class="mini" onclick="urlZeileWeg('pg-urls')" title="&minus;">&minus;</button></span></div>
      <div id="pg-urls" class="urltab" data-praefix="pages-url"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.pages.image_max"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.pages.image_max.i">i</span></span><span><input type="number" id="c-sharepoint_pages_image_max_mb" min="0" max="100"> <span class="muted small">MB</span></span></div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.planner.title">Planner</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.planner.urls.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.planner.urls.i">i</span></span><span>
        <button class="mini" onclick="urlZeile('pl-urls', '')" title="+">+</button>
        <button class="mini" onclick="urlZeileWeg('pl-urls')" title="&minus;">&minus;</button></span></div>
      <div id="pl-urls" class="urltab" data-praefix="planner-url"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.planner.attachments"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.planner.attachments.i">i</span></span><input type="checkbox" id="c-planner_attachments"></div>
    </div>

    <div class="gruppe"><h3 data-i18n="settings.speed.title">Geschwindigkeit</h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.workers"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.workers.i">i</span></span><input type="number" id="c-workers" min="1" max="8"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.mirror_workers"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.mirror_workers.i">i</span></span><input type="number" id="c-mirror_workers" min="1" max="16"></div>
    </div>
  </div>

  <div class="card">
    <h2 class="mit-info"><span data-i18n="sched.title">Zeitplan</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.i">i</span></h2>
    <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.enabled"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.enabled.i">i</span></span><input type="checkbox" id="s-enabled"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.every"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.every.i">i</span></span><span><input type="number" id="s-interval" min="5" step="5" value="60"> <span class="muted small" data-i18n="sched.minutes">Minuten</span></span></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.outlook"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.outlook.i">i</span></span><input type="checkbox" id="s-outlook"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.teams"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.teams.i">i</span></span><input type="checkbox" id="s-teams"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.onedrive"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.onedrive.i">i</span></span><input type="checkbox" id="s-onedrive"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.sharepoint"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.sharepoint.i">i</span></span><input type="checkbox" id="s-sharepoint"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.pages"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.pages.i">i</span></span><input type="checkbox" id="s-sharepoint_pages"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.planner"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.planner.i">i</span></span><input type="checkbox" id="s-planner"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.index"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.index.i">i</span></span><input type="checkbox" id="s-index"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="sched.calendar"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="sched.calendar.i">i</span></span><input type="checkbox" id="s-calendar"></div>
    </div>
    <div class="aktionen">
      <span class="small muted" id="s-next"></span>
    </div>
  </div>

  <div class="card">
    <h2 class="mit-info" id="ki-karte"><span data-i18n="settings.ollama.title">KI</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.ollama.i">i</span></h2>
    <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.ollama.use"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.ollama.i">i</span></span><input type="checkbox" id="c-ollama_enabled" onchange="ollamaSchalter()"></div>
      <p class="folgen" id="ollama-folgen"></p>
    </div>

    <div id="ollama-kinder">
      <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.ollama.url"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.ollama.url.i">i</span></span><span class="feldmitstand"><input type="text" id="c-ollama"><span class="stand hide" id="st-ollama"></span></span></div>
      </div>

      <div class="gruppe">
        <h3><span data-i18n="settings.index.title">Index</span>
          <span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.index.i">i</span></h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.index.kind"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.index.kind.i">i</span></span><select id="c-index_kind" onchange="indexart(this.value === 'both')"><option value="text" id="ix-text" data-i18n="settings.index.text">Nur Volltext</option><option value="both" id="ix-beides" data-i18n="settings.index.both">Volltext und Bedeutung</option></select></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.embed_model"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.embed_model.i">i</span></span><span class="feldmitstand"><input type="text" id="c-embed_model"><span class="stand hide" id="st-embed_model"></span></span></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.batch"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.batch.i">i</span></span><input type="number" id="c-index_batch" min="1" max="512"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.semantic_min"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.semantic_min.i">i</span></span><span><input type="number" id="c-semantic_min" min="0" max="95" step="5"> <span class="muted small">%</span></span></div>
        <p class="folgen" id="index-folgen"></p>
      </div>

      <div class="gruppe">
        <h3><span data-i18n="settings.ki.title">KI-Zusammenfassung</span>
          <span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.ki.i">i</span></h3>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.chat_model"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.chat_model.i">i</span></span><span class="feldmitstand"><input type="text" id="c-chat_model"><span class="stand hide" id="st-chat_model"></span></span></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.answer_sources"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.answer_sources.i">i</span></span><input type="number" id="c-answer_sources" min="1" max="20"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2 class="mit-info" id="mcp-karte"><span data-i18n="mcp.title">Claude (MCP)</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="mcp.i">i</span></h2>
    <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.mcp_enabled"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.mcp_enabled.i">i</span></span><input type="checkbox" id="c-mcp_enabled" onchange="speichereEinstellungen()"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.mcp_port"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.mcp_port.i">i</span></span><input type="number" id="c-mcp_port" min="1024" max="65535"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.mcp_autostart"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.mcp_autostart.i">i</span></span><input type="checkbox" id="c-mcp_autostart"></div>
    </div>
    <div class="aktionen">
      <span class="small" id="mcp-state"></span>
      <button class="mini" id="mcp-toggle" onclick="toggleMcp()">Starten</button>
    </div>
    <p class="small muted" style="margin-top:14px" data-i18n-html="mcp.code.note">In Claude Code eintragen:</p>
    <div class="mitkopie"><pre id="mcp-json"></pre>
      <button class="mini kopie" onclick="kopiere('mcp-json', this)" data-i18n="copy">Kopieren</button></div>
    <p class="small muted" data-i18n-html="mcp.desktop.note">Claude Desktop akzeptiert nur <code>command</code>-Einträge:</p>
    <div class="mitkopie"><pre id="mcp-stdio"></pre>
      <button class="mini kopie" onclick="kopiere('mcp-stdio', this)" data-i18n="copy">Kopieren</button></div>
  </div>

  <div class="card">
    <h2 class="mit-info"><span data-i18n="settings.app.title">App</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.app.i">i</span></h2>
    <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.datadir"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.datadir.i">i</span></span><span></span></div>
      <div class="feldzeile breit">
        <div class="row">
          <input type="text" id="c-data-dir" style="flex:1;min-width:280px">
          <button class="mini" onclick="setzeDatenordner()" data-i18n="settings.datadir.save">Übernehmen</button>
          <button class="mini" onclick="datenordnerZurueck()" data-i18n="settings.datadir.reset">Standard</button>
          <span class="small" id="datadir-msg"></span>
        </div>
      </div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.indexdir"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.indexdir.i">i</span></span><span></span></div>
      <div class="feldzeile breit">
        <div class="row">
          <input type="text" id="c-index-dir" style="flex:1;min-width:280px">
          <button class="mini" onclick="setzeIndexordner()" data-i18n="settings.datadir.save">Übernehmen</button>
          <button class="mini" onclick="indexordnerZurueck()" data-i18n="settings.datadir.reset">Standard</button>
          <span class="small" id="indexdir-msg"></span>
        </div>
      </div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.homedir"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.homedir.i">i</span></span><code id="home-dir" class="small">…</code></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.appdir"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.appdir.i">i</span></span><code id="app-ort" class="small">…</code></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.search_results"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.search_results.i">i</span></span><input type="number" id="c-search_results" min="5" max="100" step="5"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.analytics_skip"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.analytics_skip.i">i</span></span><span class="small muted"></span></div>
      <div class="feldzeile breit"><textarea id="c-analytics_skip" style="min-height:70px"></textarea></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.filetype_hidden"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.filetype_hidden.i">i</span></span><span><input type="text" id="c-filetype_hidden"> <button class="mini" onclick="typenZuruecksetzen()" data-i18n="settings.skip_folders.reset">Auf Vorgabe zurücksetzen</button></span></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.lang.title"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.lang.i">i</span></span><select id="c-language"></select></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="update.enabled"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="update.enabled.i">i</span></span><input type="checkbox" id="c-update_check"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.userflow_actions"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.userflow_actions.i">i</span></span><input type="number" id="c-userflow_actions" min="0" max="50"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.runs_retention_months"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.runs_retention_months.i">i</span></span><input type="number" id="c-runs_retention_months" min="1" max="120"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.log_retention_days"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.log_retention_days.i">i</span></span><input type="number" id="c-log_retention_days" min="1" max="365"></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="settings.notifications"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="settings.notifications.i">i</span></span><select id="c-notifications"><option value="off" data-i18n="settings.notifications.off"></option><option value="errors" data-i18n="settings.notifications.errors"></option><option value="all" data-i18n="settings.notifications.all"></option></select></div>
    </div>
    <div class="aktionen">
      <span class="small muted" id="update-current"></span>
      <span class="small muted" id="update-state"></span>
      <button class="mini" onclick="pruefeUpdate()" data-i18n="update.check">Jetzt prüfen</button>
      <a class="mini" id="update-link" target="_blank" rel="noopener"
         data-i18n="update.download">Release herunterladen</a>
    </div>
  </div>

  <div class="card">
    <h2 class="mit-info"><span data-i18n="expert.title">Expertenmodus</span>
      <span class="info" tabindex="0" aria-label="i" data-i18n-title="expert.i">i</span></h2>
    <div class="gruppe">
      <div class="feldzeile "><span class="bez"><span data-i18n="export.index.only"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="export.index.only.when">i</span></span><button class="mini" onclick="run({index:true}, t('job.index'))" data-i18n="expert.run">Ausführen</button></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="export.calendar.build"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="export.calendar.build.when">i</span></span><button class="mini" onclick="run({calendar:true}, t('job.calendar'))" data-i18n="expert.run">Ausführen</button></div>
      <div class="feldzeile "><span class="bez"><span data-i18n="expert.openapi"></span><span class="info" tabindex="0" aria-label="i" data-i18n-title="expert.openapi.i">i</span></span><a class="mini" href="/api/openapi" target="_blank" rel="noopener" data-i18n="expert.open">Öffnen</a></div>
    </div>
  </div>

  <div class="speichern">
    <button class="act" onclick="speichereEinstellungen()" data-i18n="settings.save">Einstellungen speichern</button>
    <span class="small" id="cfg-msg"></span>
  </div>
</section>
</main>


<!-- The log belongs to the application, not to the export tab: it also holds
     token state, MCP output and scheduler messages. Hence a bar along the
     bottom edge, reachable from everywhere and normally closed. -->
<div id="protokoll" class="zu">
  <div class="pgriff" onpointerdown="protokollZiehen(event)"></div>
  <!-- The two buttons sit INSIDE the header row, which itself toggles the
       fold – so each one stops its click event. Otherwise the log would
       fold shut on every copy. -->
  <div class="pkopf" onclick="protokollUmschalten()">
    <span class="pfeil" id="p-pfeil">▴</span>
    <strong data-i18n="log.title">Protokoll</strong>
    <span class="small muted" id="log-letzte"></span>
    <button class="mini" onclick="event.stopPropagation();kopiere('log', this)"
            data-i18n="copy">Kopieren</button>
    <button class="mini" onclick="event.stopPropagation();fehlerMelden()"
            data-i18n="report.button">Fehler melden</button>
  </div>
  <div id="log"></div>
</div>

<div id="overlay"><div class="modal" id="modal" role="dialog" aria-modal="true"></div></div>

<script type="application/json" id="i18n">/*__I18N__*/</script>
<script type="application/json" id="schritte">/*__STEPS__*/</script>
<script>
var S = null, seen = 0, dismissed = {}, offset = 0, wizardOffen = null, wizardStand = null;

/* ---------- Language ----------
   The strings arrive ready with the page (window.I18N) – no extra fetch,
   and thus no brief flash of the wrong language. */
var I18N = JSON.parse(document.getElementById('i18n').textContent);
var STR = I18N.strings || {};
var LOC = I18N.lang || 'de';
function t(key, vars){
  var text = STR[key];
  if(text == null) return key;          // missing key: visible instead of blank
  if(vars) Object.keys(vars).forEach(function(k){
    text = text.split('{' + k + '}').join(vars[k]);
  });
  return text;
}
/* Messages from the server are either raw text (output of the export
   scripts) or {k: key, v: values}. A value may itself be such a message –
   that keeps "schedule active (every 60 minutes)" one sentence instead of
   three fragments. If v carries a `minutes`, a readable `rest` duration is
   derived from it in addition: only the interface knows the language. */
function mtext(m){
  if(m == null) return '';
  if(typeof m === 'string') return m;
  if(!m.k) return String(m);
  var v = {};
  Object.keys(m.v || {}).forEach(function(k){
    // A step's structured result renders as one translated line.
    v[k] = (k === 'ergebnis' && m.v[k] && typeof m.v[k] === 'object')
      ? ergebnisText(m.v[k]) : mtext(m.v[k]);
  });
  if(m.v && m.v.minutes !== undefined) v.rest = restzeit(m.v.minutes);
  return t(m.k, v);
}

/* The labels are the same atoms the run history table uses; extras keep
   their technical names (moved, chunks, events …). */
function ergebnisText(e){
  var bits = [];
  [['new', 'ana.runs.new'], ['unchanged', 'ana.runs.unchanged'],
   ['excluded', 'ana.runs.excluded'], ['errors', 'ana.runs.errors']]
    .forEach(function(p){
      if(e[p[0]] !== undefined && e[p[0]] !== null)
        bits.push(t(p[1]) + ' ' + zahl(e[p[0]]));
    });
  Object.keys(e.extra || {}).forEach(function(k){
    bits.push(k + ' ' + zahl(e.extra[k]));
  });
  return bits.join(' · ') || '–';
}
function restzeit(min){
  if(min === null || min === undefined) return t('unit.unknown');
  if(min < 0) return t('unit.expired');
  if(min < 60) return t('unit.min', {n: min});
  var h = Math.floor(min / 60), m = min % 60;
  if(h < 24) return m ? t('unit.hoursmin', {h: h, m: m}) : t('unit.hours', {h: h});
  var d = Math.floor(h / 24); h = h % 24;
  return h ? t('unit.dayshours', {d: d, h: h}) : t('unit.days', {d: d});
}
function fuelleSprachen(){
  var sel = el('c-language');
  sel.innerHTML = '<option value="auto">' + esc(t('settings.lang.auto')) + '</option>' +
    (I18N.languages || []).map(function(l){
      return '<option value="' + esc(l.code) + '">' + esc(l.name) + '</option>';
    }).join('');
  sel.value = (S && S.config && S.config.language) || 'auto';
}
function uebersetzeSeite(){
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    el.textContent = t(el.dataset.i18n);
  });
  // Texts with markup (<code>, <strong>) – the language file delivers HTML.
  document.querySelectorAll('[data-i18n-html]').forEach(function(el){
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(function(el){
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(function(el){
    el.title = t(el.dataset.i18nTitle);
    // Groups without a visible label carry the same text as aria-label:
    // a title alone is not reliably read out by screen readers.
    if(el.hasAttribute('aria-label')) el.setAttribute('aria-label', t(el.dataset.i18nTitle));
  });
  document.title = t('app.title');
  document.documentElement.lang = LOC;
}
uebersetzeSeite();

function api(p){ return fetch(p).then(function(r){ return r.json(); }); }

/* What a box contains, the way someone would read it. In the log every line
   is its own child, and textContent would glue them together without a
   break – a log that lands in the clipboard as one single line helps
   nobody. Input fields do not pass through here but go via
   inZwischenablage(field.value, …): they carry their text elsewhere. */
function kopiertext(id){
  var e = el(id);
  if(!e) return '';
  if(e.children && e.children.length) return [].map.call(e.children,
    function(k){ return k.textContent; }).join('\n');
  return e.textContent || '';
}
function kopiere(id, knopf){
  inZwischenablage(kopiertext(id), knopf);
}
/* Into the clipboard. On 127.0.0.1 the page counts as trustworthy, so the
   clipboard API is available – but not in every browser, and not while the
   window is not in the foreground. Hence the old way as a fallback, instead
   of silently doing nothing. */
function inZwischenablage(text, knopf){
  function fertig(ok){
    var vorher = knopf.textContent;
    knopf.textContent = t(ok ? 'copy.done' : 'copy.failed');
    setTimeout(function(){ knopf.textContent = vorher; }, 1600);
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(function(){ fertig(true); },
                                            function(){ altKopieren(text, fertig); });
  } else {
    altKopieren(text, fertig);
  }
}
function altKopieren(text, fertig){
  try {
    var feld = document.createElement('textarea');
    feld.value = text;
    feld.style.position = 'fixed';
    feld.style.opacity = '0';
    document.body.appendChild(feld);
    feld.select();
    var ok = document.execCommand('copy');
    document.body.removeChild(feld);
    fertig(ok);
  } catch(e){ fertig(false); }
}
function post(p, body){
  return fetch(p, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
}
function esc(s){ return String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function el(id){ return document.getElementById(id); }

/* Only a handful of tabs: fetch data, look at data, configure. Calendar
   and address book are views onto the same inventory as the search and thus
   sit one level below; schedule and MCP are settings. */
var KANN_VERLAUF = false;   // depends on the index, see store.features
var REITER = ['export', 'suche', 'analytics', 'einstellungen'];
var SICHTEN = ['treffer', 'kalender', 'adressbuch', 'dateien'];
var offeneSicht = 'treffer';

/* ---------- UI userflow recording ----------
   The last user actions – only the KIND (tab, search, run), never content
   like search terms or names. Purely in this page's memory, gone on close;
   the list becomes visible only in the error report, as its own editable
   field. Count in the settings (userflow_actions), 0 turns it off. */
var ablauf = [];

function ablaufGrenze(){
  return (S && S.config && typeof S.config.userflow_actions === 'number')
    ? S.config.userflow_actions : 20;
}

function merke(schluessel, detail){
  var n = ablaufGrenze();
  if(n <= 0){ ablauf.length = 0; return; }
  var d = new Date();
  function zwei(x){ return (x < 10 ? '0' : '') + x; }
  ablauf.push({t: zwei(d.getHours()) + ':' + zwei(d.getMinutes()) + ':' +
                  zwei(d.getSeconds()),
               k: schluessel, d: detail || ''});
  while(ablauf.length > n) ablauf.shift();
}

function ablaufText(){
  if(ablaufGrenze() <= 0) return '';
  return ablauf.map(function(e){
    return e.t + '  ' + t(e.k) + (e.d ? ': ' + e.d : '');
  }).join('\n');
}

function tab(name){
  merke('flow.tab', name);
  REITER.forEach(function(t){
    el('tab-' + t).classList.toggle('hide', t !== name);
    document.querySelector('[data-tab=' + t + ']').classList.toggle('on', t === name);
  });
  if(name === 'suche'){ sicht(offeneSicht); ladeOrdner(); }
  if(name === 'analytics') ladeAnalytics();
}

/* "Deleted" is in truth its own view onto the same inventory – like
   calendar and address book, not a checkbox among five filters. It shares
   the hits view's list but sets the filter. */
function sicht(name){
  if(name !== offeneSicht) merke('flow.view', name);   // tab() passes the open one through
  offeneSicht = name;
  SICHTEN.forEach(function(v){
    el('sicht-' + v).classList.toggle('hide', v !== name);
  });
  document.querySelectorAll('#sichten .chip').forEach(function(c){
    c.classList.toggle('on', c.dataset.sicht === name);
  });
  // The calendar data is a few megabytes – fetch it only when someone looks.
  if(name === 'kalender' || name === 'adressbuch') ladeKalender(name);
  if(name === 'dateien') ladeDateien();
}

/* ---------- File browser: the mirrors as a tree, all from the index --- */
var dateiSicht = {root: '', path: ''};
var dateiDaten = null;

function ladeDateien(root, path){
  if(root !== undefined) dateiSicht = {root: root, path: path || ''};
  api('/api/files?root=' + encodeURIComponent(dateiSicht.root) +
      '&path=' + encodeURIComponent(dateiSicht.path))
    .then(zeichneDateien).catch(function(){});
}

function dateiGehe(i){
  var z = dateiDaten && (dateiDaten.roots ? dateiDaten.roots[i]
                                          : dateiDaten.dirs[i]);
  if(z) ladeDateien(z.root || dateiSicht.root, z.path);
}

function dateiHoch(n){
  // n path segments survive; above the level root the crumb leads back to
  // the sources screen, not to a half-empty listing.
  if(n < 0 || n < (dateiSicht.base || 0)) return ladeDateien('', '');
  var teile = dateiSicht.path.split('/').filter(Boolean).slice(0, n);
  ladeDateien(dateiSicht.root, teile.join('/'));
}

function zeichneDateien(r){
  dateiDaten = r;
  var box = el('dateien-liste'), pfad = el('dateien-pfad');
  el('dateien-suchen').classList.toggle('hide', !!r.roots);
  if(r.error){ box.innerHTML = '<p class="hint">' + esc(mtext(r.error)) + '</p>'; return; }
  if(r.roots){
    pfad.textContent = t('files.roots');
    box.innerHTML = r.roots.length ? r.roots.map(function(w, i){
      return '<div class="dateizeile" style="cursor:pointer" onclick="dateiGehe(' + i + ')">' +
        '<strong>' + esc(w.label) + '</strong>' +
        '<span class="muted small">' + esc(zahl(w.files)) + ' ' +
        esc(t('progress.unit.files')) + '</span></div>';
    }).join('') : '<p class="hint">' + esc(t('files.none')) + '</p>';
    return;
  }
  // Breadcrumbs: sources / root / folders… – depth and label come from
  // the answer, the client holds no per-root layout knowledge.
  var teile = (r.path || '').split('/').filter(Boolean);
  var basis = r.base || 0;
  dateiSicht.base = basis;
  var krumen = ['<a href="javascript:void(0)" onclick="dateiHoch(-1)">' +
                esc(t('files.roots')) + '</a>',
                '<a href="javascript:void(0)" onclick="dateiHoch(' + basis + ')">' +
                esc(r.label || dateiSicht.root) + '</a>'];
  teile.slice(basis).forEach(function(s, i){
    krumen.push('<a href="javascript:void(0)" onclick="dateiHoch(' + (basis + i + 1) + ')">' +
                esc(s) + '</a>');
  });
  pfad.innerHTML = krumen.join(' / ');
  var zeilen = (r.dirs || []).map(function(d, i){
    return '<div class="dateizeile" style="cursor:pointer" onclick="dateiGehe(' + i + ')">' +
      '<span>📁 <strong>' + esc(d.name) + '</strong></span>' +
      '<span class="muted small">' + esc(zahl(d.files)) + ' ' +
      esc(t('progress.unit.files')) + '</span></div>';
  }).concat((r.files || []).map(function(f){
    var link = '/source?root=' + encodeURIComponent(dateiSicht.root) +
               '&path=' + encodeURIComponent(f.rel);
    return '<div class="dateizeile"' +
      (f.gone ? ' title="' + esc(t('search.gone.since', {when: fmt(f.gone)})) + '"' : '') + '>' +
      '<a href="' + link + '" target="_blank"' +
      (f.gone ? ' class="muted"' : '') + '>' + esc(f.name) + '</a>' +
      (f.gone ? ' <span class="tag weg">' + esc(t('search.gone.tag')) + '</span>' : '') +
      ' <span class="muted small">' + esc(f.date || '') +
      (f.size != null ? ' · ' + esc(bytes(f.size)) : '') + '</span></div>';
  }));
  box.innerHTML = zeilen.length ? zeilen.join('')
    : '<p class="hint">' + esc(t('files.empty')) + '</p>';
}

function dateienSuchen(){
  // The browser's spot becomes the search's filter: source and folder.
  el('f-source').value = dateiSicht.root || 'all';
  var sel = el('f-folder'), pfad = dateiSicht.path || '';
  if(pfad && !Array.prototype.some.call(sel.options,
      function(o){ return o.value === pfad; })){
    var o = document.createElement('option');
    o.value = pfad; o.textContent = pfad; sel.appendChild(o);
  }
  sel.value = pfad;
  el('filter').classList.remove('hide');
  sicht('treffer'); zeigeFilterstand(); doSearch(0);
}

/* Whoever filters nothing – the normal case – should see a search field and
   a button. The number on the toggle says something is set below it;
   without it a folded-away filter would be a trap. */
function filterFelder(){
  return [el('f-person').value.trim(), el('f-source').value === 'all' ? '' : el('f-source').value,
          el('f-from').value, el('f-to').value, el('f-folder').value,
          el('f-typ').value, el('f-gone').checked ? 'gone' : ''].filter(Boolean);
}
function filterUmschalten(){
  var zu = el('filter').classList.toggle('hide');      // true = now hidden
  el('filter-auf').setAttribute('aria-expanded', zu ? 'false' : 'true');
}
function filterLeeren(){
  el('f-person').value = ''; el('f-source').value = 'all';
  el('f-from').value = ''; el('f-to').value = ''; el('f-folder').value = '';
  el('f-typ').value = ''; el('f-gone').checked = false;
  zeigeFilterstand();
}
/* An impossible date ("June 31st") is accepted by the browser but comes out
   as an empty value. Without this check the app would keep searching
   without that bound – the field looked filled, the hits lay outside it,
   and nothing said why. A swapped date range is the same case: it reliably
   yields zero hits that look like an empty archive. */
function datumPruefen(){
  var kaputt = false;
  ['f-from', 'f-to'].forEach(function(id){
    var e = el(id);
    var schlecht = !!(e.validity && e.validity.badInput);
    e.classList.toggle('fehler', schlecht);
    if(schlecht) kaputt = true;
  });
  if(kaputt) return t('search.date.bad');
  var von = el('f-from').value, bis = el('f-to').value;
  if(von && bis && von > bis) return t('search.date.turned');
  return '';
}

function zeigeFilterstand(){
  datumPruefen();                      // mark it immediately, not only on search
  var n = filterFelder().length;
  el('filter-auf').textContent = n ? t('search.filter.n', {n: n}) : t('search.filter');
  el('filter-weg').classList.toggle('hide', !n);
}

/* The pill always leads to where something can be changed. A window that
   merely explains what is missing lets nobody fix anything – and half the
   details live in the settings anyway. */
function ollamaKachel(){
  zeigeEinstellung('ki-karte');
}

/* What stands here is decided by the same check the header pill takes its
   color from – just placed next to the field where you fix it: the address,
   the embedding model, the model for the answer. */
function zeigeOllamaStand(o, lage){
  var teile = [
    ['st-ollama', lage === 'aus' ? '' : o.running ? 'ok' : 'err',
     o.running ? 'settings.stand.da' : 'settings.stand.weg'],
    ['st-embed_model', lage === 'aus' || !o.running ? '' : o.has_model ? 'ok' : 'warn',
     o.has_model ? 'settings.stand.geladen' : 'settings.stand.fehlt'],
    ['st-chat_model', lage === 'aus' || !o.running ? '' : o.has_chat_model ? 'ok' : 'warn',
     o.has_chat_model ? 'settings.stand.geladen' : 'settings.stand.fehlt']
  ];
  teile.forEach(function(z){
    var kasten = el(z[0]);
    if(!kasten) return;
    // Without a reachable Ollama, "model missing" is not information but a
    // second message about the same cause.
    kasten.className = 'stand ' + (z[1] || 'hide');
    kasten.innerHTML = z[1]
      ? '<span class="dot ' + z[1] + '"></span>' + esc(t(z[2])) : '';
  });
}

/* Turning Ollama off means: the app stops looking for it, semantic search
   and the summary fall away, and the index is built as a pure full-text
   index. Whatever has no effect without Ollama grays out here – it stays
   visible all the same, or nobody would know what they are turning off. */
var INDEX_SEMANTISCH = true;

function indexart(semantisch){
  if(semantisch && !el('c-ollama_enabled').checked) return;
  INDEX_SEMANTISCH = !!semantisch;
  el('c-index_kind').value = INDEX_SEMANTISCH ? 'both' : 'text';
  el('index-folgen').textContent = INDEX_SEMANTISCH ? '' : t('settings.index.parked');
}

function ollamaSchalter(){
  var an = el('c-ollama_enabled').checked;
  el('ollama-kinder').classList.toggle('aus', !an);
  el('ix-beides').disabled = !an;
  el('ollama-folgen').textContent = an ? '' : t('settings.ollama.folgen');
  if(!an) indexart(false);
}

function zeigeEinstellung(anker){
  // The pill at the top still leads straight to its topic – which lives in
  // the settings rather than in a tab of its own.
  tab('einstellungen');
  var ziel = document.getElementById(anker);
  if(ziel) ziel.scrollIntoView({behavior: 'smooth', block: 'start'});
}

/* ---------- Status ----------
   Labels in everyday language, the technical term in the tooltip. Whoever
   looks for "chunks" or "MCP" finds it on hover; whoever does not know the
   words need not read them to understand the state. */
function setPill(id, cls, text, tip){
  el('p-' + id).className = 'dot ' + cls;
  el('p-' + id + '-t').textContent = text;
  var knopf = el('pill-' + id);
  if(knopf) knopf.title = tip || '';
}

function renderStatus(s){
  var first = S === null;
  S = s;

  var tok = s.token, tokTip = t('pill.token.tip');
  if(tok.account) tokTip += '\n' + tok.account;
  if(!tok.present) setPill('token','err', t('pill.token.missing'), tokTip);
  else if(tok.expired) setPill('token','err', t('pill.token.expired'), tokTip);
  else if(tok.missing && tok.missing.length) setPill('token','warn', t('pill.token.scopes'), tokTip);
  else if(tok.expires_in_minutes != null) setPill('token','ok', t('pill.token.left', {rest: restzeit(tok.expires_in_minutes)}), tokTip);
  else setPill('token','ok', t('pill.token.set'), tokTip);

  var o = s.ollama;
  // Disabled is not an error but a decision – hence gray instead of red,
  // and no wizard pushing towards an install.
  // On or off – like MCP. Why it is off sits in the tooltip, and what
  // exactly is missing in the settings, next to the field where you fix it.
  // Three labels for three kinds of "not available" would mean: the same
  // answer in three words, none of which says what to do.
  var oLage = o.disabled ? 'aus' : !o.running ? 'weg' : !o.has_model ? 'modell' : 'on';
  setPill('ollama', {on: 'ok', modell: 'warn', weg: 'err', aus: ''}[oLage],
    t(oLage === 'on' ? 'pill.ollama.on' : 'pill.ollama.off'),
    t('pill.ollama.tip.' + oLage));
  zeigeOllamaStand(o, oLage);

  // The index state lives in the Analytics tab, with everything else about
  // the inventory – the same number in two places helps nobody, the copies
  // only contradict each other eventually. What the header shows are things
  // that demand an action.
  var st = s.store;

  // Three states, not two: the endpoint runs, it does not run, or access is
  // switched off entirely. "off" for everything would claim that a client
  // registered via stdio has no access – but it does.
  // Name the transport only when just that one is missing: with the
  // endpoint running both paths are open, and "MCP HTTP on" would read as
  // if stdio were excluded.
  var mcpAus = s.config && s.config.mcp_enabled === false;
  var mcpLage = mcpAus ? 'aus' : s.mcp.running ? 'on' : 'off';
  setPill('mcp', mcpLage === 'on' ? 'ok' : '',
    t('pill.mcp.' + mcpLage), t('pill.mcp.tip.' + mcpLage));

  /* Export tab */
  if(first){
    fill('cat-outlook', ['mail','calendar','contacts'], s.config.outlook_categories, 'o');
    fill('cat-teams', ['1on1','group','meeting','channels'], s.config.teams_categories, 't');
    el('c-onedrive_enabled').checked = !!s.config.onedrive_enabled;
    el('c-sharepoint_enabled').checked = !!s.config.sharepoint_enabled;
    el('c-sharepoint_pages_enabled').checked = !!s.config.sharepoint_pages_enabled;
    el('c-planner_enabled').checked = !!s.config.planner_enabled;
    fuelleSprachen();
    el('s-enabled').checked = s.config.schedule.enabled;
    el('s-interval').value = s.config.schedule.interval_minutes;
    el('s-outlook').checked = s.config.schedule.outlook;
    el('s-teams').checked = s.config.schedule.teams;
    el('s-onedrive').checked = s.config.schedule.onedrive !== false;
    el('s-sharepoint').checked = s.config.schedule.sharepoint !== false;
    el('s-sharepoint_pages').checked = s.config.schedule.sharepoint_pages !== false;
    el('s-planner').checked = s.config.schedule.planner !== false;
    el('s-index').checked = s.config.schedule.index;
    el('s-calendar').checked = s.config.schedule.calendar;
  }
  el('teams-note').textContent = checked('t').indexOf('channels') >= 0
    ? t('export.channels.note') : '';
  el('sp-export-note').textContent = el('c-sharepoint_enabled').checked
    && !(s.config.sharepoint_urls || '').trim() ? t('export.sharepoint.nourls') : '';

  var ex = s.exports, parts = [];
  function wann(iso){ return iso ? t('export.state.last', {when: fmt(iso)}) : t('export.state.never'); }
  parts.push(t('export.state.outlook', {when: wann(ex.outlook.last_run)}));
  parts.push(t('export.state.teams', {when: wann(ex.teams.last_run)}));
  parts.push(t('export.state.onedrive', {when: wann(ex.onedrive && ex.onedrive.last_run)}));
  parts.push(t('export.state.sharepoint', {when: wann(ex.sharepoint && ex.sharepoint.last_run)}));
  parts.push(t('export.state.pages', {when: wann(ex.pages && ex.pages.last_run)}));
  parts.push(t('export.state.index', {when: st.exists ? fmt(st.built_at) : t('export.state.never')}));
  el('export-state').textContent = parts.join('  ·  ');
  // The "deleted" view and the thread history need an index that knows
  // both. An old one lacks the columns – then the chip does not exist.
  var kann = (st.features || []);
  var kannGone = kann.indexOf('gone') >= 0;
  el('gone-feld').classList.toggle('hide', !kannGone);
  if(!kannGone) el('f-gone').checked = false;
  KANN_VERLAUF = kann.indexOf('thread') >= 0;
  KANN_TYP = kann.indexOf('ext') >= 0;
  zeigeOrdnerstand(s.folders || {});
  zeigeOrdnerstand(s.folders_onedrive || {}, 'od-folders-state');
  zeigeKalenderstand(s.calendars || {});
  // Fill only on the first render – otherwise the status poll would
  // overwrite every 2.5 seconds what is being typed right now.
  if(first){ el('c-data-dir').value = s.data_dir;
              el('c-index-dir').value = s.index_dir || ''; }
  el('home-dir').textContent = s.home_dir || '';
  el('app-ort').textContent = s.app_location || '';
  zeigeUpdate(s.update || {});
  fuelleEinstellungen(s.config);

  var busy = s.jobs.busy;
  el('btn-run').disabled = busy;
  el('btn-cancel').classList.toggle('hide', !busy);
  zeigeFortschritt(s.jobs);

  /* Schedule / MCP */
  el('s-next').textContent = s.schedule_enabled && s.schedule_next
    ? t('sched.next', {when: fmt(s.schedule_next)}) : t('sched.none');
  el('mcp-toggle').textContent = t(s.mcp.running ? 'mcp.stop' : 'mcp.start');
  el('mcp-toggle').disabled = mcpAus;
  el('mcp-state').textContent = mcpAus ? t('mcp.aus')
    : s.mcp.running
    ? t('mcp.running', {url: s.mcp.url, mode: t(st.semantic ? 'mcp.mode.hybrid' : 'mcp.mode.lexical')})
    : (mtext(s.mcp.error) || t('mcp.stopped'));
  el('mcp-json').textContent = JSON.stringify(s.mcp.config.http, null, 2);
  el('mcp-stdio').textContent = JSON.stringify(s.mcp.config.stdio, null, 2);
  // The answer exists only if a model can phrase it.
  // The two rear variants hang on Ollama: "similar search" must embed the
  // query, the summary additionally needs a chat model.
  modiPruefen(!!(o.running && o.has_chat_model && st.exists && st.semantic),
              !!o.disabled);

  // After a rebuild, discard the calendar data – otherwise calendar and
  // address book would keep showing the state from before the run.
  if(kalGeladen && kalStand && s.calendar && s.calendar.built_at &&
     s.calendar.built_at !== kalStand){
    kalGeladen = false; kalStand = null;
    var reiter = document.querySelector('nav [data-tab].on');
    if(reiter && reiter.dataset.tab === 'suche' &&
       (offeneSicht === 'kalender' || offeneSicht === 'adressbuch'))
      ladeKalender(offeneSicht);
  }

  if(wizardOffen && !WIZARDS[wizardOffen]){ /* self-opened window – leave it alone */ }
  else if(s.wizard && !dismissed[s.wizard]) openWizard(s.wizard);
  else if(wizardOffen) openWizard(wizardOffen);   // keep the open wizard current
  // The second branch is not optional: once the model is loaded the server
  // no longer demands a wizard (s.wizard === null). Without it the open
  // window would say "model missing" forever while the light in the header
  // has long turned green.
}

function fmt(iso){
  if(!iso) return '–';
  var d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(LOC, {dateStyle:'short', timeStyle:'short'});
}

function fill(id, keys, active, pre){
  el(id).innerHTML = keys.map(function(k){
    var box = '<label class="chk"><input type="checkbox" id="' + pre + '-' + k + '" value="' +
      k + '"' + (active.indexOf(k) >= 0 ? ' checked' : '') +
      ' onchange="saveCats()"> ' + esc(t('export.cat.' + k)) + '</label>';
    // Channels stand apart: they pull whole teams and grow the archive fast,
    // so ticking them reveals a warning right below the box.
    return k === 'channels'
      ? '<div class="chk-sep">' + box + '<p class="small chk-note" id="teams-note"></p></div>'
      : box;
  }).join('');
}
function checked(pre){
  return Array.prototype.slice.call(document.querySelectorAll('#cat-' +
    (pre === 'o' ? 'outlook' : 'teams') + ' input:checked')).map(function(i){ return i.value; });
}
function saveCats(){
  merke('flow.save', 'export');
  post('/api/config', {outlook_categories: checked('o'), teams_categories: checked('t'),
                       onedrive_enabled: el('c-onedrive_enabled').checked,
                       sharepoint_enabled: el('c-sharepoint_enabled').checked,
                       sharepoint_pages_enabled: el('c-sharepoint_pages_enabled').checked,
                       planner_enabled: el('c-planner_enabled').checked}).then(refresh);
}

/* ---------- Runs ---------- */
function run(what, label){
  what.label = label;
  merke('flow.run', label);
  post('/api/run', what).then(function(r){
    // Even a 500 says something: better the raw error line than an empty
    // alert nobody can dig into.
    if(!r.ok) alert(mtext(r.message) || String(r.error || r.message || ''));
    refresh();
  });
}
function runExport(){
  var o = checked('o').length > 0, tm = checked('t').length > 0;
  var od = el('c-onedrive_enabled').checked;
  var sp = el('c-sharepoint_enabled').checked;
  var sps = el('c-sharepoint_pages_enabled').checked;
  var pl = el('c-planner_enabled').checked;
  if(!o && !tm && !od && !sp && !sps && !pl){ alert(t('export.nothing')); return; }
  // Calendar only with Outlook: events, contacts and the reconstruction of
  // deleted events come exclusively from the mailbox.
  run({outlook:o, teams:tm, onedrive:od, sharepoint:sp, sharepoint_pages:sps,
       planner:pl, index:true, calendar:o}, t('job.export'));
}

/* ---------- Progress ----------
   Two levels: which step out of how many, and within the step as precise as
   the script knows. The Outlook export does not know its total – it
   discovers the mails as it runs. There the bar keeps moving striped and
   the line names the count, instead of inventing a percentage nobody can
   keep. */
function zeigeFortschritt(jobs){
  var kasten = el('fortschritt'), balken = document.querySelector('.balken');
  kasten.classList.toggle('hide', !jobs.busy);
  if(!jobs.busy){
    var L = jobs.last;
    el('fortschritt-text').textContent = '';
    if(L) el('log-letzte').textContent = L.ok
      ? t('log.job.done', {label: mtext(L.label), when: fmt(L.finished)})
      : t('log.job.failed', {label: mtext(L.label), when: fmt(L.finished),
                             detail: mtext(L.detail)});
    return;
  }
  var j = jobs.job || {}, n = (j.steps || []).length || 1, i = j.index || 0;
  var p = j.progress, anteil = 0, kennt = false;
  if(p && p.total){ anteil = Math.min(p.done / p.total, 1); kennt = true; }

  balken.classList.toggle('unbekannt', !kennt);
  if(kennt) el('balken-fuell').style.width = Math.round((i + anteil) / n * 100) + '%';

  // The step name arrives as a text key from the server (job.step.…); the
  // run's label was translated by the browser before it started the run.
  // mtext passes strings through unchanged – so for the step, the key
  // itself would otherwise end up in the line.
  var zeile = t('log.job.running', {label: mtext(j.label), step: t(j.step),
                                    i: i + 1, n: n});
  if(p) zeile += ' · ' + (p.total
    ? t('progress.of', {done: p.done.toLocaleString(LOC),
                        total: p.total.toLocaleString(LOC), what: einheit(p.what)})
    : t('progress.count', {done: p.done.toLocaleString(LOC), what: einheit(p.what)}));
  el('fortschritt-text').textContent = zeile;
  el('log-letzte').textContent = zeile;
}
function einheit(was){
  return was ? t('progress.unit.' + was) : '';
}

/* ---------- Log ---------- */
function protokollPlatz(){
  // The box floats fixed above the page – the content gets exactly enough
  // foot room that nothing disappears behind it: from the page's point of
  // view the log is its end.
  var haupt = document.querySelector('main');
  if(!haupt) return;
  var p = el('protokoll');
  haupt.style.paddingBottom = p.classList.contains('hide')
    ? '20px' : (p.offsetHeight + 16) + 'px';
}
function protokollUmschalten(){
  var p = el('protokoll');
  p.classList.toggle('zu');
  try { localStorage.setItem('protokoll', p.classList.contains('zu') ? 'zu' : 'auf'); } catch(e){}
  if(!p.classList.contains('zu')){
    var box = el('log'); box.scrollTop = box.scrollHeight;
  }
  protokollPlatz();
}
function protokollHoehe(h){
  var grenze = Math.max(80, Math.min(Math.round(window.innerHeight * 0.7), h));
  el('protokoll').style.setProperty('--loghoehe', grenze + 'px');
  return grenze;
}
function protokollZiehen(ev){
  ev.preventDefault();
  var start = ev.clientY, hoehe = el('log').offsetHeight;
  function bewegt(e){
    protokollHoehe(hoehe + (start - e.clientY));
    protokollPlatz();
  }
  function ende(e){
    document.removeEventListener('pointermove', bewegt);
    document.removeEventListener('pointerup', ende);
    var h = protokollHoehe(hoehe + (start - e.clientY));
    try { localStorage.setItem('protokoll_hoehe', String(h)); } catch(e2){}
  }
  document.addEventListener('pointermove', bewegt);
  document.addEventListener('pointerup', ende);
}
function stelleProtokollHer(){
  try {
    if(localStorage.getItem('protokoll') === 'auf') el('protokoll').classList.remove('zu');
    var h = parseInt(localStorage.getItem('protokoll_hoehe'), 10);
    if(h) protokollHoehe(h);
  } catch(e){}
  protokollPlatz();
}

function pullLog(){
  if(beendet) return;
  api('/api/log?since=' + seen).then(function(r){
    if(!r.lines || !r.lines.length){ seen = r.seq; return; }
    var box = el('log'), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    r.lines.forEach(function(l){
      var d = document.createElement('div');
      d.className = 'l-' + l.level;
      d.textContent = l.t + '  ' + mtext(l.text);
      box.appendChild(d);
    });
    while(box.childElementCount > 1200) box.removeChild(box.firstChild);
    if(!(S && S.jobs && S.jobs.busy)){
      var letzte = r.lines[r.lines.length - 1];
      el('log-letzte').textContent = mtext(letzte.text);
    }
    seen = r.seq;
    if(atEnd) box.scrollTop = box.scrollHeight;
  });
}

/* ---------- Error reporting ----------
   The app sends nothing. It assembles a text, lays it out in the open and
   opens the GitHub form with it – submitted by the human, in their own
   browser, under their own account.

   That is why the report sits in a text field and not in a pretty preview:
   what one is supposed to be able to change must also look like something
   changeable. What is in here is mail and chat – no pattern recognizes
   folder names and subject lines, only whoever wrote them can read them.
   Addresses and user paths are stripped by the server beforehand
   (app.anonymisiere). */
var berichtDaten = null;

function fehlerMelden(){
  berichtDaten = null;
  berichtFenster();                       // show something immediately, then fill
  api('/api/log?since=0').then(function(r){
    var zeilen = r.lines || [];
    var fehler = zeilen.filter(function(l){ return l.level === 'err'; });
    return post('/api/report', {
      log: zeilen.map(function(l){ return l.t + '  ' + mtext(l.text); }).join('\n'),
      // The last error line as the suggested title: "BrokenProcessPool …"
      // says more than "error in 4.0.0", and it gets edited anyway.
      hint: fehler.length ? mtext(fehler[fehler.length - 1].text) : ''});
  }).then(function(b){
    berichtDaten = b;
    if(wizardOffen === 'report') berichtFenster();
  });
}

/* The three fields here are the same as in the bug form on GitHub
   (.github/ISSUE_TEMPLATE/bug.yml): what, system, log. The URL prefills
   them via their field IDs – what stands here stands over there, field by
   field. */
function berichtSystem(b){
  return b.system.map(function(s){
    return t('report.sys.' + s.k) + ': ' + s.v;
  }).join('\n');
}

function berichtFenster(){
  var b = berichtDaten;
  if(!b){
    oeffneEigenes('report', modalKopf(t('report.title'), 'report') +
      '<p class="small muted">' + esc(t('report.loading')) + '</p>');
    return;
  }
  var mono = 'width:100%;margin:2px 0 10px;font-family:ui-monospace,Menlo,' +
    'Consolas,monospace;font-size:12.5px';
  var koerper =
    '<p class="small muted">' + esc(t('report.intro')) + '</p>' +
    '<label class="small" for="rep-titel">' + esc(t('report.field.title')) + '</label>' +
    '<input type="text" id="rep-titel" style="width:100%;margin:2px 0 12px" value="' +
      esc(b.title) + '">' +
    '<label class="small" for="rep-was">' + esc(t('report.body.what')) + '</label>' +
    '<textarea id="rep-was" rows="3" style="' + mono + '" placeholder="' +
      esc(t('report.body.hint')) + '"></textarea>' +
    '<label class="small" for="rep-system">' + esc(t('report.body.system')) + '</label>' +
    '<textarea id="rep-system" rows="6" spellcheck="false" style="' + mono + '">' +
      esc(berichtSystem(b)) + '</textarea>' +
    '<label class="small" for="rep-ablauf">' + esc(t('report.body.actions')) + '</label>' +
    '<textarea id="rep-ablauf" rows="4" spellcheck="false" style="' + mono + '">' +
      esc(ablaufText()) + '</textarea>' +
    '<label class="small" for="rep-log">' + esc(t('report.body.log')) + '</label>' +
    '<textarea id="rep-log" rows="8" spellcheck="false" style="' + mono + '">' +
      esc(b.log) + '</textarea>' +
    '<div class="banner warn" style="margin:0">' + esc(t('report.privacy')) + '</div>' +
    '<p class="small muted" id="rep-hinweis" style="margin:8px 0 0"></p>';
  oeffneEigenes('report', modalKopf(t('report.title'), 'report') + koerper +
    modalFuss({text: t('report.open'), tun: 'berichtOeffnen()'},
              {text: t('copy'),
               tun: 'inZwischenablage(berichtGesamt(), this)'}));
}

/* For the clipboard: the fields as one readable text. */
function berichtGesamt(){
  return t('report.field.title') + ': ' + el('rep-titel').value + '\n\n' +
    t('report.body.what') + ':\n' + el('rep-was').value + '\n\n' +
    t('report.body.system') + ':\n' + el('rep-system').value + '\n\n' +
    t('report.body.actions') + ':\n' + el('rep-ablauf').value + '\n\n' +
    t('report.body.log') + ':\n' + el('rep-log').value + '\n';
}

/* GitHub receives the prefilled fields in the URL. Too-long URLs are
   rejected by the server – with a blank page, not with an explanation. So
   trim beforehand and say so, instead of chancing it. */
var URL_GRENZE = 7000;

function berichtAdresse(basis, titel, was, system, aktionen, log){
  var gekuerzt = false;
  // The truncation notice has to be measured along. Appending it only at
  // the end would mean exceeding the limit by exactly its length.
  function adresse(){
    return basis + '?template=bug.yml' +
      '&title=' + encodeURIComponent(titel) +
      '&what=' + encodeURIComponent(was) +
      '&system=' + encodeURIComponent(system) +
      '&actions=' + encodeURIComponent(aktionen) +
      '&log=' + encodeURIComponent(gekuerzt ? t('report.cut') + '\n' + log : log);
  }
  var url = adresse();
  // Cut from the TOP of the log: the last lines are the ones that matter.
  // A report missing the crash would be no report.
  while(url.length > URL_GRENZE){
    var schnitt = log.indexOf('\n');
    if(schnitt < 0) break;
    log = log.slice(schnitt + 1);
    gekuerzt = true;
    url = adresse();
  }
  if(url.length > URL_GRENZE){          // giant lines or giant fields
    was = was.slice(0, 1000);
    system = system.slice(0, 1500);
    aktionen = aktionen.slice(-1000);
    log = log.slice(-2000);
    gekuerzt = true;
    url = adresse();
  }
  return {url: url, gekuerzt: gekuerzt};
}

function berichtOeffnen(){
  if(!berichtDaten) return;
  var ziel = berichtAdresse(berichtDaten.url,
                            el('rep-titel').value.trim() || t('report.title.fallback'),
                            el('rep-was').value, el('rep-system').value,
                            el('rep-ablauf').value, el('rep-log').value);
  el('rep-hinweis').textContent = ziel.gekuerzt ? t('report.truncated') : '';
  window.open(ziel.url, '_blank', 'noopener');
}

/* ---------- Search ---------- */
/* Fetch the folder list once: it only changes on indexing, and a select
   that reloads on every keystroke would be pure load. */
var ordnerJeQuelle = {}, typenJeQuelle = {}, ordnerStand = null;

/* What the empty choice is labeled. "All folders" is wrong for calendars
   and chat kinds – and a select that names itself wrongly reads like a
   bug. */
var ORDNER_ALLE = {
  kalender: 'search.folder.all.kalender',
  teams:    'search.folder.all.teams',
  kontakte: 'search.folder.all.kontakte',
  planner:  'search.folder.all.planner',
  sharepoint: 'search.folder.all.sharepoint',
  pages:    'search.folder.all.pages'
};

/* The four Teams kinds are named in the index after their storage folder.
   That is right for filtering and unreadable for display – here are the
   names a human knows them by, in their language rather than in whichever
   one happened to be set while indexing. */
function ordnerName(pfad){
  var s = t('search.folder.teams.' + pfad);
  return s === 'search.folder.teams.' + pfad ? pfad : s;
}

/* A select with a single entry is no choice: it filters nothing away. So
   the field disappears – left grayed out it looked broken, and the one
   folder is already implied by the source anyway (for "contacts" it always
   is, since hardly any mailbox has contact folders).

   Folder and file type share this: both hang on the source, both disappear
   when there is nothing to choose, and neither may leave a choice standing
   that does not exist in the new source. */
function fuelleAuswahl(id, liste, alle){
  var sel = el(id), vorher = sel.value;
  var wahl = liste.length > 1;
  sel.classList.toggle('hide', !wahl);
  sel.innerHTML = '<option value="">' + esc(alle) + '</option>' +
    (wahl ? liste.map(function(e){
      return '<option value="' + esc(e.wert) + '">' + esc(e.name) +
             ' (' + e.zahl.toLocaleString(LOC) + ')</option>';
    }).join('') : '');
  sel.value = wahl && liste.some(function(e){ return e.wert === vorher; })
    ? vorher : '';
  // The counter on "Filter" must notice the dropped choice – the list only
  // arrives after the source has been switched.
  zeigeFilterstand();
}

function zeichneOrdner(liste){
  fuelleAuswahl('f-folder', liste.map(function(f){
    return {wert: f.path, name: ordnerName(f.path), zahl: f.messages};
  }), t(ORDNER_ALLE[el('f-source').value] || 'search.folder.all'));
}

/* File types exist only where attachments or files exist – chats, events
   and contacts have none. And only if the index knows the column: an older
   one does not, and then the field is absent entirely instead of filtering
   into the void. */
var KANN_TYP = false;
function typenMoeglich(quelle){
  return KANN_TYP && (quelle === 'all' || quelle === 'outlook' ||
                      quelle === 'onedrive' || quelle === 'sharepoint');
}
function zeichneTypen(liste){
  fuelleAuswahl('f-typ', liste.map(function(e){
    return {wert: e.type, name: e.type.toUpperCase(), zahl: e.messages};
  }), t('search.type.all'));
}

function ladeOrdner(){
  var quelle = el('f-source').value || 'all';
  // After an index run the lists are different: new folders, new counts.
  var stand = (S && S.store) ? S.store.built_at : null;
  if(stand !== ordnerStand){ ordnerJeQuelle = {}; typenJeQuelle = {}; ordnerStand = stand; }
  ladeTypen(quelle);
  if(ordnerJeQuelle[quelle]){ zeichneOrdner(ordnerJeQuelle[quelle]); return; }
  api('/api/folders?limit=300&source=' + encodeURIComponent(quelle))
    .then(function(r){
      ordnerJeQuelle[quelle] = r.folders || [];
      if((el('f-source').value || 'all') === quelle) zeichneOrdner(ordnerJeQuelle[quelle]);
    }).catch(function(){});
}

function ladeTypen(quelle){
  if(!typenMoeglich(quelle)){ zeichneTypen([]); return; }
  if(typenJeQuelle[quelle]){ zeichneTypen(typenJeQuelle[quelle]); return; }
  api('/api/filetypes?limit=40&source=' + encodeURIComponent(quelle))
    .then(function(r){
      typenJeQuelle[quelle] = r.filetypes || [];
      if((el('f-source').value || 'all') === quelle) zeichneTypen(typenJeQuelle[quelle]);
    }).catch(function(){});
}

function trefferProSeite(){
  var n = S && S.config ? parseInt(S.config.search_results, 10) : NaN;
  return isNaN(n) ? 20 : Math.max(5, Math.min(n, 100));
}
/* Searching happens when someone asks for it – with the button or with
   Enter. Not while typing and not when setting a filter: one should be able
   to enter term, person, date range and folder in peace, without a search
   firing after every change. The filters only report their state to the
   toggle above. */
/* The search mode. Text search is the default and stays it after every
   start: it is the only one that always works, and the only one whose
   result is predictable. The other two are a deliberate detour.

   What the interface calls "text search" is mode=lexical on the server;
   "similar search" is semantic. The AI variant uses hybrid: there one types
   a question, and BM25 alone is bad at finding whole questions. */
var SUCHMODUS = 'text';
var MODUS_ZU_SERVER = {text: 'lexical', aehnlich: 'semantic', ki: 'hybrid'};
var TREFFER_OFFEN = true;

function suchmodus(art){
  SUCHMODUS = art;
  ['text', 'aehnlich', 'ki'].forEach(function(a){
    el('m-' + a).classList.toggle('on', a === art);
  });
  el('q').placeholder = t('search.ph.' + art);
  TREFFER_OFFEN = art !== 'ki';
  el('ki-klappe').classList.toggle('hide', art !== 'ki');
  el('results').classList.toggle('hide', !TREFFER_OFFEN);
  el('pager').classList.toggle('hide', !TREFFER_OFFEN);
  if(el('q').value.trim() || filterFelder().length) doSearch(0);
  else abbrechenKI();
}

/* Without Ollama the two rear variants stay visible but dead. Hiding them
   would mean: whoever never sees them never learns they exist. */
function modiPruefen(moeglich, abgeschaltet){
  ['aehnlich', 'ki'].forEach(function(a){
    var b = el('m-' + a);
    b.disabled = !moeglich;
    b.title = moeglich ? '' : t(abgeschaltet ? 'search.mode.off' : 'search.mode.needs');
  });
  el('modus-fehlt').textContent = t(abgeschaltet ? 'search.mode.off' : 'search.mode.needs');
  el('modus-fehlt').classList.toggle('hide', moeglich);
  if(!moeglich && SUCHMODUS !== 'text') suchmodus('text');
}

function klappeTreffer(){
  TREFFER_OFFEN = !TREFFER_OFFEN;
  el('results').classList.toggle('hide', !TREFFER_OFFEN);
  el('pager').classList.toggle('hide', !TREFFER_OFFEN);
  zeigeKlappknopf();
}
function zeigeKlappknopf(n){
  if(n === undefined) n = el('results').querySelectorAll('.hit').length;
  el('ki-klappknopf').textContent = TREFFER_OFFEN
    ? t('search.ki.hide') : t('search.ki.show', {n: n});
}

function sofortSuchen(){
  doSearch(0);
}

function doSearch(off){
  var fehler = datumPruefen();
  if(fehler){
    el('results').innerHTML = '<div class="banner err">' + esc(fehler) + '</div>';
    el('pager').classList.add('hide');
    return;
  }
  offset = off || 0;
  // Only the kind and the number of filters – never the search text or a name.
  var filter = ['f-person', 'f-source', 'f-from', 'f-to', 'f-folder', 'f-typ']
    .filter(function(id){ return el(id).value; }).length +
    (el('f-gone').checked ? 1 : 0);
  merke('flow.search', MODUS_ZU_SERVER[SUCHMODUS] + (filter ? ' +' + filter : ''));
  var proSeite = trefferProSeite();
  var p = new URLSearchParams({q: el('q').value, person: el('f-person').value,
    source: el('f-source').value, from: el('f-from').value, to: el('f-to').value,
    gone: el('f-gone').checked ? '1' : '', folder: el('f-folder').value,
    filetype: el('f-typ').value,
    mode: MODUS_ZU_SERVER[SUCHMODUS], k: proSeite, offset: offset});
  zeigeFilterstand();
  el('results').textContent = t('search.running');
  api('/api/search?' + p.toString()).then(function(r){
    renderHits(r);
    // Only the AI variant asks the model – and only after the hits are in.
    // The search is instant, the model takes a minute; whoever looks up an
    // invoice number with text search never has to deal with it.
    if(SUCHMODUS === 'ki' && (r.results || []).length) frageKI();
    else abbrechenKI();
  });
}
/* Why a hit is a hit must be visible. The preview shows the excerpt around
   the match; here the term inside it also gets highlighted. Escape first,
   then mark – the other way round the markup itself would be escaped and
   sit as a literal <mark> in the text. */
function hervor(text){
  var roh = el('q').value.trim();
  var h = esc(text);
  // Only text search matches literally. In semantic search a highlight
  // would be a claim: there the meaning fits, not the word.
  if(!roh || SUCHMODUS !== 'text') return h;
  roh.split(/\s+/).filter(Boolean).forEach(function(w){
    var muster = new RegExp('(' + esc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    h = h.replace(muster, '<mark>$1</mark>');
  });
  return h;
}

/* One menu per hit, but only ever one open. A click elsewhere and ESC close
   it – without that it would stay standing while paging. */
var offenesMenu = null;
function menuZu(){
  if(offenesMenu === null) return;
  var m = el('menu-' + offenesMenu);
  if(m){ m.classList.add('hide');
         m.previousElementSibling.setAttribute('aria-expanded', 'false'); }
  offenesMenu = null;
}
function menuAuf(ev, i){
  ev.stopPropagation();
  var war = offenesMenu;
  menuZu();
  if(war === i) return;                    // the same button closes it again
  el('menu-' + i).classList.remove('hide');
  ev.currentTarget.setAttribute('aria-expanded', 'true');
  offenesMenu = i;
}
document.addEventListener('click', menuZu);

function filterPerson(wer){
  menuZu();
  el('f-person').value = wer;
  el('filter').classList.remove('hide');
  doSearch(0);
}

/* ---------- Person-field suggestions ----------
   A free-text field against a fixed inventory: whoever types "Meier" where
   "Meyer" is stored gets zero hits and cannot tell whether the person is
   missing or misspelled. The list answers that before the search – and
   names the message count for each name, so the right suggestion stands out
   when two similar ones exist. */
var VORSCHLAG_MAX = 5;
var personVorschlaege = [], personAktiv = -1, personTimer = null;

function personVorschlagen(){
  clearTimeout(personTimer);
  var wort = el('f-person').value.trim();
  if(wort.length < 2){ personZu(); return; }
  // Do not query on every keystroke: the query runs over all people in the
  // archive, and whoever types a name does so in one go.
  personTimer = setTimeout(function(){
    api('/api/people?limit=' + VORSCHLAG_MAX +
        '&source=' + encodeURIComponent(el('f-source').value) +
        '&contains=' + encodeURIComponent(wort)).then(function(r){
      // Typed on in the meantime: this answer is stale.
      if(el('f-person').value.trim() !== wort) return;
      personZeichnen(r.people || [], r.total_distinct || 0, r.total_messages || 0);
    }).catch(personZu);
  }, 150);
}

/* The list forces no choice: the person search is a substring search, it
   just does not look like one. Hence a row below the names that spells it
   out – "schmi*" instead of one particular Schmidt. It carries the same
   figure as the rows above (messages, not people), otherwise two units
   would sit in one list. */
function personZeichnen(liste, gesamt, nachrichten){
  var wort = el('f-person').value.trim();
  var stern = wort.charAt(wort.length - 1) === '*' ? wort : wort + '*';
  // With exactly one hit, "all" would be the same hit once more.
  var mitStern = gesamt > 1;
  personVorschlaege = liste.map(function(p){
    return {wert: p.name, name: p.name, zahl: p.messages};
  });
  if(mitStern) personVorschlaege.push({wert: stern, name: stern,
                                       zahl: nachrichten, alle: true});
  personAktiv = -1;
  var kasten = el('personliste');
  kasten.innerHTML = liste.length
    ? personVorschlaege.map(function(p, i){
        return (p.alle ? '<hr>' : '') +
          '<button type="button" role="option" aria-selected="false" ' +
          'id="personwahl-' + i + '" onclick="personWaehlen(' + i + ')">' +
          '<span class="wer' + (p.alle ? ' alle' : '') + '">' + esc(p.name) +
          '</span><span class="zahl">' + p.zahl.toLocaleString(LOC) +
          '</span></button>';
      }).join('') +
      // More names than slots: say so instead of silently cutting off –
      // otherwise the five would pass for all there are.
      (gesamt > liste.length
        ? '<div class="leer">' + esc(t('search.person.more',
                                       {n: gesamt - liste.length})) + '</div>'
        : '')
    : '<div class="leer">' + esc(t('search.person.none')) + '</div>';
  kasten.classList.remove('hide');
  el('f-person').setAttribute('aria-expanded', 'true');
}

function personZu(){
  clearTimeout(personTimer);
  personVorschlaege = [];
  personAktiv = -1;
  el('personliste').classList.add('hide');
  el('f-person').setAttribute('aria-expanded', 'false');
}

function personWaehlen(i){
  var p = personVorschlaege[i];
  if(!p) return;
  el('f-person').value = p.wert;
  personZu();
  zeigeFilterstand();
}

function personHervor(i){
  personAktiv = i;
  personVorschlaege.forEach(function(_, j){
    var b = el('personwahl-' + j);
    if(b) b.setAttribute('aria-selected', j === i ? 'true' : 'false');
  });
}

/* Keyboard as in any suggestion list: up, down, Enter, Esc. Without it one
   would have to reach for the mouse to take over a name. */
function personTaste(ev){
  var offen = !el('personliste').classList.contains('hide');
  var n = personVorschlaege.length;
  if(ev.key === 'Escape' && offen){ personZu(); ev.preventDefault(); return; }
  if(!offen || !n) return;
  if(ev.key === 'ArrowDown'){
    personHervor((personAktiv + 1) % n); ev.preventDefault();
  } else if(ev.key === 'ArrowUp'){
    // From "nothing selected" (-1), ↑ belongs at the end of the list. Done
    // arithmetically it would jump to the second-to-last entry.
    personHervor(personAktiv <= 0 ? n - 1 : personAktiv - 1); ev.preventDefault();
  } else if(ev.key === 'Enter' && personAktiv >= 0){
    personWaehlen(personAktiv); ev.preventDefault();
  }
}

document.addEventListener('click', function(ev){
  var feld = el('f-person');
  if(!ev.target || !feld) return personZu();
  if(ev.target === feld) return;
  var knoten = ev.target;
  while(knoten){
    if(knoten.id === 'personliste') return;   // clicked inside the box
    knoten = knoten.parentElement;
  }
  personZu();
});

/* Similar items to exactly this hit. Unlike the similar search this needs
   no Ollama: the vector of this passage sits ready in the index, nothing
   has to be embedded. That is why the entry is available even while the
   variant above is grayed out. */
function aehnlicheZu(cid){
  menuZu();
  el('q').value = '';
  el('results').textContent = t('search.running');
  api('/api/similar?cid=' + encodeURIComponent(cid) +
      '&k=' + trefferProSeite()).then(function(r){
    offset = 0;
    renderHits(r);
    abbrechenKI();
  });
}

function renderHits(r){
  if(r.error){ el('results').innerHTML = '<span class="err">' + esc(mtext(r.error)) + '</span>'; return; }
  var hits = r.results || [];
  if(!hits.length){ el('results').textContent = t('search.nohits');
                    el('pager').innerHTML = ''; abbrechenKI(); return; }
  // "Find similar" hangs on the VECTORS in the index, not on Ollama: the
  // vector of this passage sits ready, nothing gets embedded.
  // Without vectors – a pure full-text index – the entry would run into the
  // void, so it stands grayed out instead of vanishing.
  var aehnlichMoeglich = !!(S && S.store && S.store.semantic);
  // The tag speaks the interface language – the server label is only the
  // fallback for sources this page does not know yet.
  function quellTag(h){
    var key = h.source === 'datei'
      ? (h.root === 'sharepoint' ? 'search.source.sharepoint'
                                 : 'search.source.onedrive')
      : 'search.source.' + h.source;
    var wert = t(key);
    return wert === key ? (h.source_label || h.source || '') : wert;
  }
  el('results').innerHTML = hits.map(function(h, i){
    var m = /^o365:\/\/([^/]+)\/(.*)$/.exec(h.uri || '');
    var link = m ? '/source?root=' + m[1] + '&path=' + m[2] : null;
    var faden = h.thread && KANN_VERLAUF ? esc(h.thread).replace(/'/g, "\\'") : '';
    return '<div class="hit" id="treffer-' + (i + 1) + '">' +
      '<h3><span class="fussnote">[' + (i + 1) + ']</span>' +
      (link ? '<a href="' + link + '" target="_blank">' : '') +
      esc(h.title || t('search.nosubject')) + (link ? '</a>' : '') + '</h3>' +
      '<div class="wer"><span class="tag">' + esc(quellTag(h)) + '</span>' +
      (h.gone ? '<span class="tag weg" title="' +
        esc(t('search.gone.since', {when: fmt(h.gone)})) + '">' +
        esc(t('search.gone.tag')) + '</span>' : '') + esc(h.who || '') + '</div>' +
      '<div class="wann">' + esc(h.date || '') + '</div>' +
      '<div class="menuzelle">' +
        '<button class="punkte-knopf" aria-haspopup="true" aria-expanded="false" ' +
        'aria-label="' + esc(t('search.menu')) + '" onclick="menuAuf(event,' + i + ')">⋯</button>' +
        '<div class="menu hide" id="menu-' + i + '">' +
          (link ? '<a class="mini" href="' + link + '" target="_blank" ' +
                  'style="text-decoration:none;border:0;padding:7px 10px">' +
                  esc(t('search.menu.source')) + '</a>'
                : '<button disabled>' + esc(t('search.menu.source')) + '</button>') +
          '<button' + (faden ? ' onclick="zeigeVerlauf(' + (i + 1) + ',\'' + faden + '\')"'
                             : ' disabled') + '>' +
            esc(t('search.menu.thread')) + '</button>' +
          '<button' + (h.cid && aehnlichMoeglich
                       ? ' onclick="aehnlicheZu(\'' + esc(h.cid) + '\')"'
                       : ' disabled title="' + esc(t('search.menu.similar.aus')) + '"') +
            '>' + esc(t('search.menu.similar')) + '</button>' +
          (h.who ? '<hr><button onclick="filterPerson(\'' +
                   esc(h.who).replace(/'/g, "\\'") + '\')">' +
                   esc(t('search.menu.person')) + '</button>' : '') +
        '</div>' +
      '</div>' +
      '<div class="prev">' + hervor(h.preview || '') + '…</div>' +
      '<div class="verlauf" id="verlauf-' + (i + 1) + '"></div>' +
      '</div>';
  }).join('');
  // Paging follows the setting too – otherwise "next" would skip hits or
  // show the same ones again.
  var proSeite = trefferProSeite();
  el('pager').innerHTML =
    (offset > 0 ? '<button class="ghost" onclick="doSearch(' + Math.max(0, offset - proSeite) + ')">' + esc(t('search.back')) + '</button>' : '') +
    (hits.length >= proSeite ? '<button class="ghost" onclick="doSearch(' + (offset + proSeite) + ')">' + esc(t('search.next')) + '</button>' : '');
  // No ranking line here: a search without a term has no ranking at all,
  // and "hybrid" is a word for developers. Which mode runs is stated by the
  // toggle at the top.
  if(SUCHMODUS === 'ki') zeigeKlappknopf(hits.length);
}

/* A hit alone often says too little: "yes, let's do it that way" only means
   something with the question before it. So the thread unfolds below the
   hit instead of jumping to another view. */
function zeigeVerlauf(nr, schluessel){
  var kasten = el('verlauf-' + nr);
  if(!kasten) return;
  kasten.innerHTML = '<p class="hint">' + esc(t('cal.loading')) + '</p>';
  api('/api/thread?key=' + encodeURIComponent(schluessel)).then(function(r){
    if(r.error || !(r.messages || []).length){
      kasten.innerHTML = '<p class="hint">' + esc(t('search.thread.alone')) + '</p>';
      return;
    }
    kasten.innerHTML = '<div class="verlaufliste"><p class="small muted">' +
      esc(t('search.thread.count', {n: r.count})) + '</p>' +
      r.messages.map(function(m){
        var g = /^o365:\/\/([^/]+)\/(.*)$/.exec(m.uri || '');
        var link = g ? '/source?root=' + g[1] + '&path=' + g[2] : null;
        return '<div class="vzeile"><span class="vdatum">' + esc(m.date || '') + '</span>' +
          '<span class="vwer">' + esc(m.who || '') + '</span>' +
          (link ? '<a href="' + link + '" target="_blank">' : '<span>') +
          esc(m.title || t('search.nosubject')) + (link ? '</a>' : '</span>') +
          '</div>';
      }).join('') + '</div>';
  }).catch(function(e){
    kasten.innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
  });
}

/* =======================================================================
   Calendar and address book – views from combined_search.py, here against
   /api/calendar instead of embedded data. The evaluation itself (including
   the events reconstructed from mails) is done by combined_search.py.
   ======================================================================= */
var KAL = null, kalGeladen = false, kTimer = null, kalStand = null;
var DAYMS = 86400000;
/* Weekday and month names come from the browser for the chosen language –
   they do not belong in the language files. */
var WD = wochentage(), MON = monatsnamen();
var STATI = ['confirmed','tentative','cancelled','deleted','gone'];
function wochentage(){
  var f = new Intl.DateTimeFormat(LOC, {weekday: 'short'});
  return [0,1,2,3,4,5,6].map(function(i){ return f.format(new Date(Date.UTC(2024, 0, 1 + i))); });
}
function monatsnamen(){
  var f = new Intl.DateTimeFormat(LOC, {month: 'long'});
  return [0,1,2,3,4,5,6,7,8,9,10,11].map(function(i){ return f.format(new Date(Date.UTC(2024, i, 15))); });
}
function stl(st){ return t('cal.st.' + (STATI.indexOf(st) >= 0 ? st : 'confirmed')); }
var events = [], byDay = new Map(), REBUILT = [], contacts = [];
var calMode = 'week', cursor = new Date(), rbSt = 'all';

function toks(q){ return q.toLowerCase().split(/\s+/).filter(Boolean); }
function allIn(hay, worte){ hay = (hay||'').toLowerCase();
  return worte.every(function(x){ return hay.indexOf(x) >= 0; }); }
function quelle(r){ return '/source?root=' + encodeURIComponent(r.root||'outlook') +
                           '&path=' + encodeURIComponent(r.rel||''); }

function ladeKalender(ziel){
  if(kalGeladen) return zeichneKalenderTeil(ziel);
  kalGeladen = true;
  api('/api/calendar').then(function(d){
    if(d.error){
      var h = '<p class="hint">' + esc(mtext(d.error)) + '</p>' +
        '<button class="act" onclick="run({calendar:true}, t(&quot;job.calendar&quot;))">' +
        esc(t('cal.build.now')) + '</button>';
      el('kalBox').innerHTML = h; el('kbBox').innerHTML = h;
      kalGeladen = false;                     // try again after the build
      return;
    }
    KAL = d;
    // Remember the same value the status delivers (file time) – d.generated
    // sits in the JSON and would never match, the comparison would always
    // fire.
    kalStand = (S && S.calendar) ? S.calendar.built_at : null;
    var recs = d.recs || [];
    events = recs.filter(function(r){ return r.src === 'kalender' && r.ts != null; });
    ladePersonen();
    contacts = recs.filter(function(r){ return r.src === 'kontakte'; })
      .sort(function(a,b){ return (a.title||'').localeCompare(b.title||'',LOC,{sensitivity:'base'}); });
    REBUILT = events.filter(function(r){ return r.st === 'deleted' || r.st === 'gone'; });
    verteileAufTage();
    setzeStartwoche();
    var c = d.counts || {};
    el('kalStats').textContent = t('cal.stats', {n: c.kalender, r: c.rekonstruiert,
                                                 when: fmt(d.generated)});
    zeichneKalenderTeil(ziel);
  }).catch(function(e){
    // Without this branch the promise swallows every error and the view
    // stays at "loading…" forever – exactly that has happened once.
    kalGeladen = false;
    var h = '<p class="hint err">' + esc(String(e && e.message || e)) + '</p>';
    el('kalBox').innerHTML = h; el('kbBox').innerHTML = h;
  });
}
function zeichneKalenderTeil(ziel){
  if(!KAL) return;
  if(ziel === 'adressbuch') drawBook(); else drawCal();
}

// Distribute events onto days (multi-day ones appear on each day)
function verteileAufTage(){
  byDay = new Map();
  events.forEach(function(r){
    var s = midnight(r.ts * 1000);
    var endMs = (r.te != null ? r.te : r.ts) * 1000;
    if(r.ad) endMs -= DAYMS;            // DTEND is exclusive for all-day events
    var e = midnight(Math.max(endMs, r.ts * 1000));
    for(var d = new Date(s), n = 0; d <= e && n < 366; d = addDays(d,1), n++){
      var k = dkey(d);
      if(!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(r);
    }
  });
  byDay.forEach(function(list){
    list.sort(function(a,b){ return (a.ad?0:1) - (b.ad?0:1) || a.ts - b.ts; });
  });
}
function setzeStartwoche(){
  // An archive mostly lies in the past: jump to the most recent event
  if(!events.length) return;
  var last = events.reduce(function(m,r){ return r.ts > m ? r.ts : m; }, -Infinity);
  if(last * 1000 < midnight(Date.now()).getTime()) cursor = new Date(last * 1000);
}

function dkey(d){ return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') +
                         '-' + String(d.getDate()).padStart(2,'0'); }
function midnight(ms){ var d = new Date(ms); d.setHours(0,0,0,0); return d; }
function addDays(d,n){ var x = new Date(d); x.setDate(x.getDate()+n); return x; }
function startOfWeek(d){ return addDays(midnight(d.getTime()), -((d.getDay()+6)%7)); }
function hhmm(d){ return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0'); }
function isoWeek(d){
  var tag = midnight(d.getTime()); tag.setDate(tag.getDate() + 3 - ((tag.getDay()+6)%7));
  var w1 = new Date(tag.getFullYear(), 0, 4);
  return 1 + Math.round(((tag - w1)/DAYMS - 3 + ((w1.getDay()+6)%7))/7);
}
function evTime(r){
  if(r.ad) return t('cal.allday');
  var s = hhmm(new Date(r.ts*1000));
  if(r.te != null && r.te > r.ts) s += '–' + hhmm(new Date(r.te*1000));
  return s;
}
function evHtml(r){
  var st = STATI.indexOf(r.st) >= 0 ? r.st : 'confirmed';
  var tip = [r.title, stl(st), r.d,
             r.loc ? t('cal.tip.location', {v: r.loc}) : '',
             r.who ? t('cal.tip.organizer', {v: r.who}) : '',
             (r.att && r.att.length) ? t('cal.tip.attendees', {v: r.att.join(', ')}) : '',
             r.ctx].filter(Boolean).join('\n');
  return '<a class="ev ' + st + '" href="' + quelle(r) + '" target="_blank" rel="noopener" title="' +
         esc(tip) + '"><span class="evt">' + esc(evTime(r)) + '</span> ' + esc(r.title) + '</a>';
}
function dayCell(d, extraCls){
  var k = dkey(d), list = byDay.get(k) || [];
  var today = k === dkey(new Date()) ? ' today' : '';
  return '<div class="day' + (extraCls||'') + today + '">' +
         '<div class="dnum"><b>' + d.getDate() + '</b><span class="wd">' + WD[(d.getDay()+6)%7] + '</span></div>' +
         (list.length ? list.map(evHtml).join('') : '') + '</div>';
}

/* Events reconstructed from mails only – their own list instead of the calendar grid */
function rbRow(r){
  var d = new Date(r.ts*1000);
  return '<a class="rbrow ' + r.st + '" href="' + quelle(r) + '" target="_blank" rel="noopener" title="' +
         esc(r.ctx) + '"><span class="rbdate">' + WD[(d.getDay()+6)%7] + ' ' + esc(r.d) + '</span>' +
         '<span class="rbstate">' + esc(t(r.st === 'deleted' ? 'cal.rb.state.deleted' : 'cal.rb.state.gone')) + '</span>' +
         '<span class="rbtitle">' + esc(r.title) + '</span>' +
         '<span class="rbwho">' + esc(r.who) + '</span></a>';
}
function rbFrame(){
  var nDel = REBUILT.filter(function(r){ return r.st === 'deleted'; }).length;
  el('kalBox').innerHTML =
      '<p class="rbnote">' + esc(t('cal.rb.note')) + '</p><div class="calbar">' +
      '<span class="chip" data-rb="all">' + esc(t('cal.rb.all', {n: REBUILT.length})) + '</span>' +
      '<span class="chip" data-rb="deleted">' + esc(t('cal.rb.deleted', {n: nDel})) + '</span>' +
      '<span class="chip" data-rb="gone">' + esc(t('cal.rb.gone', {n: REBUILT.length - nDel})) + '</span>' +
      '<input type="text" id="rbQ" placeholder="' + esc(t('cal.rb.search.ph')) + '" style="min-width:240px">' +
      '<span class="rbcount"></span></div><div id="rblist"></div>';
  el('rbQ').addEventListener('input', function(){ clearTimeout(kTimer); kTimer = setTimeout(rbList, 160); });
  document.querySelectorAll('#kalBox [data-rb]').forEach(function(ch){
    ch.addEventListener('click', function(){ rbSt = ch.dataset.rb; rbList(); });
  });
}
function rbList(){
  var worte = toks(el('rbQ').value.trim());
  var hits = REBUILT.filter(function(r){
    return (rbSt === 'all' || r.st === rbSt) &&
           (!worte.length || allIn(r.title + ' ' + (r.ppl||'') + ' ' + (r.x||''), worte));
  });
  document.querySelectorAll('#kalBox [data-rb]').forEach(function(c){
    c.classList.toggle('on', c.dataset.rb === rbSt);
  });
  document.querySelector('#kalBox .rbcount').textContent = t('cal.rb.hits', {n: hits.length});
  var h = '', monat = null;
  hits.forEach(function(r){
    var d = new Date(r.ts*1000), m = MON[d.getMonth()] + ' ' + d.getFullYear();
    if(m !== monat){ h += '<div class="rbmonth">' + m + '</div>'; monat = m; }
    h += rbRow(r);
  });
  // Empty does not always mean the same: "nothing found" would be a lie
  // when nothing was searched because reconstruction is switched off.
  var leer = (KAL && KAL.reconstruct === false) ? 'cal.rb.off' : 'cal.rb.empty';
  el('rblist').innerHTML = h || '<p class="hint">' +
    esc(t(REBUILT.length ? 'cal.rb.nohits' : leer)) + '</p>';
}

function drawCal(){
  el('kalNav').classList.toggle('hide', calMode === 'rebuilt');
  el('kalLegend').classList.toggle('hide', calMode === 'rebuilt');   // the rows carry their own labels
  if(calMode === 'rebuilt'){
    el('kalTitle').textContent = '';
    if(!document.querySelector('#kalBox [data-rb]')) rbFrame();
    return rbList();
  }
  if(!events.length){
    el('kalTitle').textContent = '';
    el('kalBox').innerHTML = '<p class="hint">' + esc(t('cal.empty')) + '</p>';
    return;
  }
  var head = '<div class="grid ' + (calMode === 'week' ? 'wk' : 'mo') + ' dowrow" style="margin-bottom:2px">' +
             WD.map(function(w){ return '<div class="dow">' + w + '</div>'; }).join('') + '</div>';
  var cells = '';
  if(calMode === 'week'){
    var mon = startOfWeek(cursor), sun = addDays(mon, 6);
    for(var i = 0; i < 7; i++) cells += dayCell(addDays(mon, i));
    el('kalTitle').textContent = t('cal.kw', {week: isoWeek(mon),
      from: mon.getDate() + '. ' + MON[mon.getMonth()],
      to: sun.getDate() + '. ' + MON[sun.getMonth()] + ' ' + sun.getFullYear()});
  } else {
    var first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    var start = startOfWeek(first);
    var lastDay = new Date(cursor.getFullYear(), cursor.getMonth()+1, 0);
    var weeks = Math.round((startOfWeek(lastDay) - start)/DAYMS/7) + 1;
    for(var j = 0; j < weeks*7; j++){
      var d = addDays(start, j);
      cells += dayCell(d, d.getMonth() !== cursor.getMonth() ? ' out' : '');
    }
    el('kalTitle').textContent = MON[cursor.getMonth()] + ' ' + cursor.getFullYear();
  }
  el('kalBox').innerHTML = head + '<div class="grid ' + (calMode === 'week' ? 'wk' : 'mo') + '">' + cells + '</div>';
}
el('kalPrev').addEventListener('click', function(){
  cursor = calMode === 'week' ? addDays(cursor, -7)
                              : new Date(cursor.getFullYear(), cursor.getMonth()-1, 1);
  drawCal();
});
el('kalNext').addEventListener('click', function(){
  cursor = calMode === 'week' ? addDays(cursor, 7)
                              : new Date(cursor.getFullYear(), cursor.getMonth()+1, 1);
  drawCal();
});
el('kalToday').addEventListener('click', function(){ cursor = new Date(); drawCal(); });
document.querySelectorAll('#sicht-kalender .calbar .chip[data-mode]').forEach(function(ch){
  ch.addEventListener('click', function(){
    document.querySelectorAll('#sicht-kalender .calbar .chip[data-mode]')
      .forEach(function(x){ x.classList.remove('on'); });
    ch.classList.add('on'); calMode = ch.dataset.mode;
    if(calMode !== 'rebuilt') el('kalBox').innerHTML = '';   // discard the list's frame
    drawCal();
  });
});

/* ---------- Analytics ----------
   Two cards with two origins: what is on top comes from the index and is
   there instantly. What is below asks Microsoft – and therefore happens
   only at the press of a button. */
var anaGeladen = false;

function ladeAnalytics(neu){
  if(anaGeladen && !neu) return;
  anaGeladen = true;
  if(neu){
    // Visible feedback for the refresh button: back to the loading hint
    // until the fresh numbers arrive. Refresh recomputes on the server –
    // the ONE way to invalidate everything at once.
    el('ana-kpi').innerHTML = '<p class="hint">' + esc(t('cal.loading')) + '</p>';
    el('ana-kpi-dateien').innerHTML = '';
    el('ana-runs').innerHTML = '<p class="hint">' + esc(t('cal.loading')) + '</p>';
  }
  (neu ? post('/api/analytics-refresh', {}) : api('/api/analytics'))
    .then(zeigeAnalytics).catch(function(e){
    el('ana-kpi').innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
  });
  api('/api/runs?limit=50').then(function(r){ renderRuns(r.runs || []); })
    .catch(function(e){
      el('ana-runs').innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
    });
}

/* ---------- Run history ----------
   One row per app-driven run, expandable to the per-step details. The data
   comes from runs.db (see run_history.py); counts and durations only. */
function durationText(s){
  if(s === null || s === undefined) return '–';
  if(s < 60) return Math.round(s) + ' s';
  return (s / 60).toFixed(s < 600 ? 1 : 0) + ' min';
}

// Step metadata from the registry (steps.py), injected on delivery – a new
// export needs no hand-maintenance here.
var SCHRITTE = JSON.parse(document.getElementById('schritte').textContent || '{}');

function quelleName(key){
  var q = (SCHRITTE[key] || {}).quelle;
  if(!q) return null;
  return q.indexOf('.') >= 0 ? t(q) : q;   // i18n key or plain name
}

function runElements(r){
  var e = r.elements || {}, parts = [];
  // Which categories, in brackets – "(all)" when every one was enabled.
  function detail(cats, alle){
    var namen = cats.length >= alle.length ? [t('ana.runs.all')]
      : cats.map(function(c){ return t('export.cat.' + c); });
    return ' (' + namen.join(', ') + ')';
  }
  if((e.outlook || []).length)
    parts.push('Outlook' + detail(e.outlook, ['mail', 'calendar', 'contacts']));
  if((e.teams || []).length)
    parts.push('Teams' + detail(e.teams, ['1on1', 'group', 'meeting', 'channels']));
  Object.keys(SCHRITTE).forEach(function(k){
    if(k === 'outlook' || k === 'teams') return;
    if(e[k]) parts.push(quelleName(k) + ' (' + t('ana.runs.all') + ')');
  });
  (r.steps || []).forEach(function(s){
    if(s.key === 'index' && parts.indexOf('Index') < 0) parts.push('Index');
  });
  return parts.join(', ') || '–';
}

function runStepLine(s){
  if(s.skipped) return t(s.label) + ': ' + t('ana.runs.skipped');
  var bits = [];
  if(s.duration_s !== null && s.duration_s !== undefined) bits.push(durationText(s.duration_s));
  if(s.new !== null && s.new !== undefined) bits.push(t('ana.runs.new') + ' ' + zahl(s.new));
  if(s.unchanged !== null && s.unchanged !== undefined)
    bits.push(t('ana.runs.unchanged') + ' ' + zahl(s.unchanged));
  if(s.excluded) bits.push(t('ana.runs.excluded') + ' ' + zahl(s.excluded));
  if(s.errors) bits.push(t('ana.runs.errors') + ' ' + zahl(s.errors));
  if(s.ok === 0) bits.push(t('ana.runs.failed'));
  return t(s.label) + ': ' + (bits.join(' · ') || '–');
}

function renderRuns(runs){
  var box = el('ana-runs');
  if(!runs.length){
    box.innerHTML = '<p class="hint">' + esc(t('ana.runs.empty')) + '</p>';
    return;
  }
  var ok = runs.filter(function(r){ return r.result === 'done'; }).length;
  var html = '<p class="small muted">' +
    esc(t('ana.runs.count', {n: runs.length, ok: ok})) + '</p>' +
    '<table class="anatab"><thead><tr>' +
    ['time', 'origin', 'elements', 'duration', 'new', 'result']
      .map(function(k){ return '<th>' + esc(t('ana.runs.col.' + k)) + '</th>'; })
      .join('') + '</tr></thead><tbody>';
  runs.forEach(function(r, i){
    var dauer = (r.finished_at && r.started_at) ? r.finished_at - r.started_at : null;
    // "New" counts the exports only – index and calendar report their own
    // numbers, but those describe derived artefacts, not new archive items.
    var neu = null, neuJe = [];
    (r.steps || []).forEach(function(s){
      if(quelleName(s.key) && s.new !== null && s.new !== undefined){
        neu = (neu || 0) + s.new;
        neuJe.push(quelleName(s.key) + ': ' + zahl(s.new));
      }
    });
    html += '<tr class="lauf" style="cursor:pointer" onclick="toggleRun(' + i + ')">' +
      '<td>' + esc(new Date(r.started_at * 1000).toLocaleString(LOC)) + '</td>' +
      '<td>' + esc(t('ana.runs.origin.' +
                     (r.origin === 'schedule' ? 'schedule' : 'manual'))) + '</td>' +
      '<td>' + esc(runElements(r)) + '</td>' +
      '<td>' + esc(durationText(dauer)) + '</td>' +
      '<td' + (neuJe.length ? ' title="' + esc(neuJe.join('\n')) + '"' : '') +
      '>' + esc(zahl(neu)) + '</td>' +
      '<td>' + esc(t('ana.runs.result.' + (r.result || 'running'))) + '</td></tr>' +
      '<tr class="hide" id="lauf-details-' + i + '"><td colspan="6" class="small muted">' +
      (r.steps || []).map(runStepLine).map(esc).join('<br>') +
      '<div class="row" style="margin-top:8px"><button class="mini" ' +
      'onclick="event.stopPropagation();zeigeRunLog(' + r.id + ', ' + i + ')">' +
      esc(t('ana.runs.log')) + '</button></div>' +
      '<div id="lauf-log-' + i + '" class="lauflog hide"></div></td></tr>';
  });
  box.innerHTML = html + '</tbody></table>';
}

function toggleRun(i){
  var d = el('lauf-details-' + i);
  if(d) d.classList.toggle('hide');
}

function zeigeRunLog(id, i){
  // The stored log of one run, inline below its steps. Second click folds
  // it away again; the lines come from runs.db and translate on display,
  // like the live log bar.
  var box = el('lauf-log-' + i);
  if(!box.classList.contains('hide')){ box.classList.add('hide'); return; }
  box.classList.remove('hide');
  box.innerHTML = '<p class="hint">' + esc(t('cal.loading')) + '</p>';
  api('/api/run-log?id=' + id).then(function(r){
    var zeilen = r.lines || [];
    if(!zeilen.length){
      box.innerHTML = '<p class="hint">' + esc(t('ana.runs.log.empty')) + '</p>';
      return;
    }
    box.innerHTML = zeilen.map(function(l){
      return '<div class="l-' + esc(l.level) + '">' +
        esc(new Date(l.ts * 1000).toLocaleTimeString(LOC)) + '  ' +
        esc(mtext(l.text)) + '</div>';
    }).join('');
  }).catch(function(e){
    box.innerHTML = '<p class="hint">' + esc(String(e)) + '</p>';
  });
}

function bytes(n){
  if(!n) return '–';
  var e = ['B','KB','MB','GB','TB'], i = 0;
  while(n >= 1024 && i < e.length - 1){ n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + e[i];
}
function zahl(n){
  // null means "don't know" – 0 would mean "none".
  return (n === null || n === undefined) ? '–' : Number(n).toLocaleString(LOC);
}
/* Two different things live under a tile: NUMBERS (the split by source, how
   large the index is) and EXPLANATIONS (what a conversation is, why deleted
   items still sit there). Only the numbers belong there permanently; the
   explanation is read once. Hence `hinweis` stays visible and `tip` goes to
   the info sign. */
function kachelHtml(wert, titel, hinweis, tip, klick){
  var info = tip ? ' <span class="info" tabindex="0" title="' + esc(tip) +
                   '" role="img" aria-label="Info">i</span>' : '';
  return '<div class="kpi' + (klick ? ' klickbar" role="button" tabindex="0"' +
             ' onclick="' + klick + '" onkeydown="if(event.key===\'Enter\')' + klick + '"'
           : '"') + '>' +
    '<div class="kpi-wert">' + esc(wert) + '</div>' +
    '<div class="kpi-titel">' + esc(titel) + info + '</div>' +
    (hinweis ? '<div class="kpi-hint">' + esc(hinweis) + '</div>' : '') + '</div>';
}


/* ---------- Charts ----------
   Hand-drawn SVG instead of a library: the bundle should not grow by a
   charting package, and the three shapes here are simple. Deliberately
   spare – thin marks, restrained axes, numbers only where they are needed.
   The tooltip sits in <title>: every browser shows it and every screen
   reader speaks it, without an extra layer for it. */

/* Stacked monthly bars: Teams, mail, everything else. A gap is a missing
   column – which is why the series includes the empty months too. */
function verlaufDia(reihe){
  if(!reihe.length) return '';
  var B = 720, H = 110, U = 16;
  var hoch = Math.max.apply(null, reihe.map(function(r){ return r.gesamt; })) || 1;
  var breite = B / reihe.length, lueck = reihe.length > 120 ? 0 : Math.min(2, breite * 0.25);
  var teile = reihe.map(function(r, i){
    var x = i * breite, y = H - U, s = '';
    [['outlook', 'var(--serie-b)'], ['teams', 'var(--serie-a)']]
      .forEach(function(paar){
        var h = (r[paar[0]] / hoch) * (H - U);
        if(h <= 0) return;
        y -= h;
        s += '<rect x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '" width="' +
             Math.max(0.5, breite - lueck).toFixed(2) + '" height="' + h.toFixed(2) +
             '" fill="' + paar[1] + '"/>';
      });
    return '<g><title>' + esc(r.m + ': ' + zahl(r.gesamt)) + '</title>' +
      '<rect x="' + x.toFixed(2) + '" y="0" width="' + breite.toFixed(2) +
      '" height="' + (H - U) + '" fill="transparent"/>' + s + '</g>';
  }).join('');
  // Year boundaries as ticks – month labels would be mush at 90 columns.
  var marken = reihe.map(function(r, i){
    return r.m.slice(5) !== '01' ? ''
      : '<text class="tick" x="' + (i * breite + 2).toFixed(1) + '" y="' + (H - 4) + '">' +
        r.m.slice(0, 4) + '</text>';
  }).join('');
  return '<svg class="dia" viewBox="0 0 ' + B + ' ' + H + '" role="img" aria-label="' +
    esc(t('ana.verlauf')) + '">' + teile +
    '<line class="achse" x1="0" y1="' + (H - U) + '" x2="' + B + '" y2="' + (H - U) + '"/>' +
    marken + '</svg>';
}

/* Growth: the same time axis, but its own picture. Putting both quantities
   into ONE drawing would mean two scales side by side – which reliably
   misleads. */
function wachstumDia(reihe){
  if(reihe.length < 2) return '';
  var B = 720, H = 90, U = 16, hoch = reihe[reihe.length - 1].summe || 1;
  var punkte = reihe.map(function(r, i){
    return (i * (B / (reihe.length - 1))).toFixed(2) + ',' +
           ((H - U) - (r.summe / hoch) * (H - U)).toFixed(2);
  }).join(' ');
  return '<svg class="dia" viewBox="0 0 ' + B + ' ' + H + '" role="img" aria-label="' +
    esc(t('ana.wachstum')) + '">' +
    '<polyline class="linie" points="' + punkte + '"/>' +
    '<line class="achse" x1="0" y1="' + (H - U) + '" x2="' + B + '" y2="' + (H - U) + '"/>' +
    '<text class="tick" x="0" y="' + (H - 4) + '">' + esc(reihe[0].m) + '</text>' +
    '<text class="tick" x="' + B + '" y="' + (H - 4) + '" text-anchor="end">' +
    esc(reihe[reihe.length - 1].m + ' · ' + zahl(hoch)) + '</text></svg>';
}

/* Ranking as horizontal bars – for everything that is pure quantity. */
function rangListe(eintraege, nenner){
  if(!eintraege.length) return '';
  var hoch = Math.max.apply(null, eintraege.map(function(e){ return e.n; })) || 1;
  // The rows sit as columns in ONE grid, not as separate grids next to each
  // other – only that way do all bars start at the same place.
  return '<div class="rangliste">' + eintraege.map(function(e){
    return '<span class="name" title="' + esc(e.name) + '">' + esc(e.name) + '</span>' +
      '<span class="bal"><i style="width:' + ((e.n / hoch) * 100).toFixed(1) + '%"></i></span>' +
      '<span class="zahl">' + esc(nenner ? nenner(e.n) : zahl(e.n)) + '</span>';
  }).join('') + '</div>';
}

function diaBlock(titel, sub, inhalt){
  if(!inhalt) return '';
  return '<div class="dia-titel">' + esc(titel) + '</div>' +
    (sub ? '<p class="dia-sub">' + esc(sub) + '</p>' : '') + inhalt;
}

function zeigeVerlaeufe(a){
  var v = a.verlauf || [];
  var luecken = (a.luecken || []).map(function(l){
    return l.monate === 1 ? l.von : l.von + '–' + l.bis;
  });
  var legende = '<div class="legende">' +
    '<span><i style="background:var(--serie-a)"></i>' + esc(t('search.source.teams')) + '</span>' +
    '<span><i style="background:var(--serie-b)"></i>' + esc(t('search.source.outlook')) + '</span></div>';
  el('ana-dia').innerHTML =
    diaBlock(t('ana.verlauf'), t('ana.verlauf.sub'), legende + verlaufDia(v)) +
    (luecken.length
      ? '<p class="dia-sub" style="margin-top:6px">' +
        esc(t('ana.luecken', {n: luecken.length, liste: luecken.join(', ')})) + '</p>'
      : (v.length ? '<p class="dia-sub" style="margin-top:6px">' +
                    esc(t('ana.luecken.keine')) + '</p>' : '')) +
    diaBlock(t('ana.wachstum'), t('ana.wachstum.sub'), wachstumDia(v)) +
    diaBlock(t('ana.typen'), t('ana.typen.sub'),
             rangListe((a.anhang_typen || []).map(function(x){
               // The catch-all needs a name: "…" says nothing, and it is
               // often larger than the entries above it.
               return {name: x.typ === '…' ? t('ana.typen.rest') : x.typ, n: x.n};
             }))) +
    diaBlock(t('ana.dateitypen'), t('ana.dateitypen.sub'),
             rangListe((a.datei_typen || []).map(function(x){
               return {name: x.typ, n: x.n}; }))) +
    diaBlock(t('ana.dateien'), t('ana.dateien.sub'),
             rangListe((a.grosse_dateien || []).map(function(d){
               return {name: d.pfad, n: d.bytes}; }), bytes)) +
    diaBlock(t('ana.people'), t('ana.personen.sub'),
             rangListe((a.top_personen || []).map(function(pe){
               return {name: pe.who, n: pe.n}; })));
}

function zeigeAnalytics(a){
  el('ana-stand').textContent = a.built_at
    ? t('ana.stand', {when: fmt(a.built_at)}) : '';
  zeigeBerichte(a);
  if(!a.exists){
    el('ana-kpi').innerHTML = '<p class="hint">' + esc(t('search.sub.none')) + '</p>';
    el('ana-kpi-dateien').innerHTML = '';
    el('ana-dia').innerHTML = '';
    return;
  }
  var k = a.komm || {}, je = {};
  (a.quellen || []).forEach(function(q){ je[q.src] = q.n; });
  var zeitraum = (k.von && k.bis)
    ? fmtTag(k.von) + ' – ' + fmtTag(k.bis) : '–';
  // Clickable only when there is something to show – a tile that leads into
  // an empty search at zero hits is a dead end.
  var klick = k.verschwunden ? 'zeigeVerschwundene()' : '';
  el('ana-kpi').innerHTML =
    kachelHtml(zahl(k.nachrichten), t('ana.messages'),
               t('search.source.teams') + ' ' + zahl(je.teams || 0) + ' · ' +
               t('search.source.outlook') + ' ' + zahl(je.outlook || 0)) +
    kachelHtml(zahl(k.gespraeche), t('ana.threads'), '', t('ana.threads.hint')) +
    kachelHtml(zahl(k.mit_anhang), t('ana.attachments'), '', t('ana.attachments.hint')) +
    kachelHtml(zahl(k.personen), t('ana.people')) +
    kachelHtml(zeitraum, t('ana.period')) +
    kachelHtml(zahl(k.verschwunden), t('ana.gone'), '',
               t(klick ? 'ana.gone.hint.klick' : 'ana.gone.hint'), klick);
  var d = a.dateien || {}, g = a.groesse || {};
  var teile = [];
  if(d.onedrive) teile.push('OneDrive ' + zahl(d.onedrive));
  if(d.sharepoint)
    teile.push(t('search.source.sharepoint') + ' ' + zahl(d.sharepoint));
  var pl = a.planner || {};
  el('ana-kpi-dateien').innerHTML =
    // No mirror, no file tiles – "0 files" tells nobody anything.
    (d.n ? kachelHtml(zahl(d.n), t('ana.files'), teile.join(' · '),
                      t('ana.files.hint')) : '') +
    (d.pages ? kachelHtml(zahl(d.pages), t('ana.pages')) : '') +
    (pl.n ? kachelHtml(zahl(pl.n), t('ana.planner'), '',
                       t('ana.planner.hint')) : '') +
    (d.n || d.pages || pl.n
      ? kachelHtml(zahl((d.verschwunden || 0) + (pl.verschwunden || 0)),
                   t('ana.gone.files'), '', t('ana.gone.files.hint')) : '') +
    kachelHtml(bytes((g.teams || 0) + (g.outlook || 0) + (g.onedrive || 0) +
                     (g.sharepoint || 0) + (g.pages || 0) + (g.planner || 0)),
               t('ana.size'), t('ana.size.hint', {index: bytes(g.index)}));
  zeigeVerlaeufe(a);
}

function zeigeBerichte(a){
  // One block per report, straight from the registry – adding a checkable
  // source is one line there, not a fourth hand-wired box.
  el('ana-checks').innerHTML = Object.keys(BERICHTKAESTEN).map(function(feld){
    return berichtHtml(a[feld], BERICHTKAESTEN[feld].titel,
                       feld !== 'vollstaendigkeit');
  }).join('');
}

function fmtTag(ts){
  return new Date(ts * 1000).toLocaleDateString(LOC, {year: 'numeric', month: 'short'});
}

function zeigeVerschwundene(){
  el('f-gone').checked = true;
  el('q').value = ''; el('f-person').value = '';
  el('filter').classList.remove('hide');   // otherwise an invisible filter takes effect
  tab('suche'); sicht('treffer'); zeigeFilterstand(); doSearch(0);
}

var BERICHTKAESTEN = {
  vollstaendigkeit:            {titel: 'ana.check.title.mail'},
  vollstaendigkeit_onedrive:   {titel: 'ana.check.title.onedrive'},
  vollstaendigkeit_sharepoint: {titel: 'ana.check.title.sharepoint'},
  vollstaendigkeit_pages:      {titel: 'ana.check.title.pages'}
};

function berichtHtml(b, titelKey, od){
  // od: all mirror/pages reports speak of files instead of mails.
  // The mailbox gets the "never checked" hint; for a mirror a block would
  // otherwise stand there permanently although the source is not used at
  // all.
  if(!b){ return od ? '' :
            '<p class="hint">' + esc(t('ana.check.none')) + '</p>'; }
  var titel = '<h3 style="margin:14px 0 6px;font-size:14px">' +
              esc(t(titelKey)) + '</h3>';
  var luecken = (b.ordner || []).filter(function(z){ return z.fehlt > 0; });
  var kopf = titel + '<p class="' + (b.fehlt ? 'warnzeile' : 'okzeile') + '">' +
    esc(t(b.fehlt ? (od ? 'ana.check.gaps.files' : 'ana.check.gaps')
                  : 'ana.check.complete',
          {n: zahl(b.fehlt), erwartet: zahl(b.erwartet), da: zahl(b.vorhanden),
           weg: zahl(b.geloescht)})) + '</p>' +
    '<p class="small muted">' + esc(t('ana.check.when', {when: fmt(b.geprueft)})) + '</p>' +
    // Without this line it would look as if 20,000 mails were missing. They
    // are not – they were never fetched because the selection skips them.
    (b.ausgelassen ? '<p class="small muted">' +
      esc(t(od ? 'ana.check.skipped.files' : 'ana.check.skipped',
            {n: zahl(b.ausgelassen),
                                  ordner: (b.ausgelassene_ordner || []).join(', ')})) +
      '</p>' : '');
  if(!luecken.length){ return kopf; }
  return kopf + '<table class="anatab"><thead><tr>' +
    '<th>' + esc(t('ana.check.folder')) + '</th><th>' + esc(t('ana.check.expected')) +
    '</th><th>' + esc(t('ana.check.present')) + '</th><th>' + esc(t('ana.check.missing')) +
    '</th></tr></thead><tbody>' +
    luecken.slice(0, 30).map(function(z){
      return '<tr><td>' + esc(z.ordner) + '</td><td>' + zahl(z.erwartet) +
        '</td><td>' + zahl(z.vorhanden) + '</td><td class="fehlt">' + zahl(z.fehlt) +
        '</td></tr>';
    }).join('') + '</tbody></table>';
}

/* One button, not two. "Check" is a question to the archive, not to one
   source – whoever sees two buttons first has to decide what they actually
   want to know. OneDrive only comes along when it is used, though:
   otherwise it would be a network request for an answer nobody cares
   about. */
function nutztOneDrive(){
  return !!((S.config && S.config.onedrive_enabled) ||
            (S.folders_onedrive && S.folders_onedrive.abgeglichen));
}
function nutztSharePoint(){
  return !!(S.config && S.config.sharepoint_enabled &&
            (S.config.sharepoint_urls || '').trim());
}
function nutztPages(){
  return !!(S.config && S.config.sharepoint_pages_enabled &&
            (S.config.sharepoint_pages_urls || '').trim());
}
function pruefeVollstaendigkeit(){
  el('ana-check-state').textContent = t('ana.check.running');
  post('/api/run', {check: true, check_onedrive: nutztOneDrive(),
                    check_sharepoint: nutztSharePoint(),
                    check_pages: nutztPages(),
                    label: 'job.check'}).then(function(r){
    if(!r.ok){ el('ana-check-state').textContent = mtext(r.message); return; }
    warteAufLauf();
  });
}
function warteAufLauf(){
  wennLaufFertig(function(){
    el('ana-check-state').textContent = '';
    ladeAnalytics(true);
  });
}

/* ---------- Address book ---------- */
function telHref(t){ return 'tel:' + (t||'').replace(/[^\d+]/g, ''); }
function cardHtml(e){
  var r = e.c || {};
  var sub = [r.role, r.org].filter(Boolean).join(' · ');
  // Only entries from the address book have a source file to link to.
  var h = '<div class="card2"><div class="cname">' +
    (e.c ? '<a href="' + quelle(r) + '" target="_blank" rel="noopener">' +
           esc(e.name) + '</a>' : esc(e.name)) +
    (e.quelle === 'comm' ? '<span class="tag herkunft">' + esc(t('book.tag.comm')) +
                           '</span>' : '') + '</div>';
  if(sub) h += '<div class="crole">' + esc(sub) + '</div>';
  (r.em||[]).forEach(function(m){
    h += '<div class="cline"><span>✉</span><a href="mailto:' + esc(m) + '">' + esc(m) + '</a></div>'; });
  (r.tel||[]).forEach(function(x){
    h += '<div class="cline"><span>☎</span><a href="' + esc(telHref(x)) + '">' + esc(x) + '</a></div>'; });
  if(e.n) h += '<div class="cline muted small">' + esc(t('book.messages', {n: e.n.toLocaleString(LOC)})) + '</div>';
  h += '<button class="mini" onclick="zeigeKommunikation(' +
       JSON.stringify(e.name).replace(/"/g, '&quot;') + ')">' +
       esc(t('book.show.comm')) + '</button>';
  return h + '</div>';
}
/* Two sources for the same question "who is that?": the Outlook address
   book (.vcf, curated, often incomplete) and the communication itself
   (senders and recipients, complete, but without phone numbers). Mixing
   them without saying so would be the worst solution – hence a filter on
   top. */
var personen = [], personenGeladen = false, bookF = 'all';

function ladePersonen(){
  if(personenGeladen) return;
  personenGeladen = true;
  api('/api/people?limit=2000').then(function(r){
    personen = r.people || [];
    if(offeneSicht === 'adressbuch') drawBook();
  }).catch(function(){ personen = []; });
}

function normName(n){ return (n || '').trim().toLowerCase(); }

function buchEintraege(){
  /* Contacts win: they carry company, role and phone number. The message
     count is added from the communication – also for those who are in the
     address book. */
  var nachName = {};
  contacts.forEach(function(c){
    nachName[normName(c.title)] = {c: c, name: c.title, quelle: 'contacts', n: 0};
  });
  personen.forEach(function(p){
    var k = normName(p.name);
    if(!k) return;
    if(nachName[k]){ nachName[k].n = p.messages; nachName[k].quelle = 'both'; }
    else nachName[k] = {c: null, name: p.name, quelle: 'comm', n: p.messages};
  });
  return Object.keys(nachName).map(function(k){ return nachName[k]; });
}

function drawBook(){
  var alle = buchEintraege();
  if(!alle.length){
    el('kbStats').textContent = '';
    el('kbBox').innerHTML = '<p class="hint">' + esc(t('book.empty')) + '</p>';
    return;
  }
  var imFilter = alle.filter(function(e){
    if(bookF === 'contacts') return e.quelle !== 'comm';
    if(bookF === 'comm') return e.quelle !== 'contacts';
    return true;
  });
  var worte = toks(el('kbQ').value.trim());
  var hits = imFilter.filter(function(e){
    var r = e.c || {};
    return !worte.length || allIn([e.name, r.org, r.role, (r.em||[]).join(' '),
                                   (r.tel||[]).join(' ')].join(' '), worte);
  });
  hits.sort(function(a, b){
    return (a.name || '').localeCompare(b.name || '', LOC, {sensitivity: 'base'});
  });
  document.querySelectorAll('#sicht-adressbuch .calbar .chip[data-book]')
    .forEach(function(c){ c.classList.toggle('on', c.dataset.book === bookF); });
  el('kbStats').textContent = t('book.stats', {n: hits.length, total: alle.length});
  if(!hits.length){ el('kbBox').innerHTML = '<p class="hint">' + esc(t('book.nohits')) + '</p>'; return; }
  var h = '', letter = null;
  hits.forEach(function(e){
    var first = (e.name || '#').trim().charAt(0).toUpperCase();
    var L = /[A-ZÄÖÜ]/.test(first) ? first : '#';
    if(L !== letter){ h += (letter !== null ? '</div>' : '') + '<div class="letter">' + L +
                           '</div><div class="cards">'; letter = L; }
    h += cardHtml(e);
  });
  el('kbBox').innerHTML = h + '</div>';
}
el('kbQ').addEventListener('input', function(){ clearTimeout(kTimer); kTimer = setTimeout(drawBook, 120); });
document.querySelectorAll('#sicht-adressbuch .calbar .chip[data-book]').forEach(function(ch){
  ch.addEventListener('click', function(){ bookF = ch.dataset.book; drawBook(); });
});

/* From a person to everything that happened with them. The search's person
   filter can do that already – this merely wires it to the address book. */
function zeigeKommunikation(name){
  el('f-person').value = name;
  el('q').value = '';
  el('f-source').value = 'all';
  el('f-gone').checked = false;
  sicht('treffer');
  doSearch(0);
}

/* ---------- Written answer ----------
   Complements the hits, does not replace them: the list below stays
   untouched, and every footnote [1] jumps exactly there. What stands here
   was written by a local model from those very hits – and the box says
   so. */
var kiLauf = null, kiQuellen = [];

function abbrechenKI(){
  if(kiLauf){ kiLauf.abort(); kiLauf = null; }
  el('ai-box').classList.add('hide');
  markiereZitate([]);
}
function kiKopf(modell, laufend){
  // The box sits above the hits – so the header must say in one sentence
  // that an AI is writing here, that it runs via Ollama on this machine,
  // and that it rests on the hits below.
  return '<div class="ahead">' +
    '<span class="tag">' + esc(t('search.ai.tag')) + '</span>' +
    '<span>' + esc(t('search.ai.label')) + '</span>' +
    (modell ? '<code class="small">' + esc(t('search.ai.model', {model: modell})) + '</code>' : '') +
    (laufend ? '<button class="ghost" style="margin-left:auto;padding:3px 10px" ' +
               'onclick="abbrechenKI()">' + esc(t('search.ai.stop')) + '</button>' : '') +
    '</div>';
}
function kiFuss(){
  return '<div class="afoot">' + esc(t('search.ai.note')) + '</div>';
}

function frageKI(){
  abbrechenKI();
  var box = el('ai-box');
  box.classList.remove('hide', 'err');
  box.innerHTML = kiKopf('', true) +
    '<div class="atext blink" id="ai-text"></div>';

  kiLauf = new AbortController();
  var text = '', modell = '';
  fetch('/api/answer', {
    method: 'POST', signal: kiLauf.signal,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({q: el('q').value, person: el('f-person').value,
                          source: el('f-source').value,
                          from: el('f-from').value, to: el('f-to').value})
  }).then(function(r){
    if(!r.ok || !r.body){
      return r.json().then(function(d){ throw new Error(mtext(d.error)); });
    }
    var leser = r.body.getReader(), dekoder = new TextDecoder(), rest = '';
    function weiter(){
      return leser.read().then(function(st){
        if(st.done) return fertig();
        rest += dekoder.decode(st.value, {stream: true});
        var zeilen = rest.split('\n');
        rest = zeilen.pop();
        zeilen.forEach(function(z){
          if(!z.trim()) return;
          var d;
          try { d = JSON.parse(z); } catch(e){ return; }
          if(d.sources){ kiQuellen = d.sources; modell = d.model;
                         box.innerHTML = kiKopf(modell, true) +
                           '<div class="atext blink" id="ai-text"></div>'; }
          if(d.text){ text += d.text; el('ai-text').textContent = text; }
          if(d.error){ throw new Error(t(d.error === 'model'
                         ? 'search.ai.err.model' : 'search.ai.err.ollama',
                         {detail: d.detail || ''})); }
        });
        return weiter();
      });
    }
    function fertig(){
      kiLauf = null;
      box.innerHTML = kiKopf(modell, false) +
        '<div class="atext">' + mitFussnoten(text) + '</div>' + kiFuss();
      markiereZitate(zitierte(text));
    }
    return weiter();
  }).catch(function(e){
    kiLauf = null;
    if(e && e.name === 'AbortError') return;      // stopped by the user
    box.classList.add('err');
    box.innerHTML = kiKopf(modell, false) +
      '<div class="atext">' + esc(String(e && e.message || e)) + '</div>';
  });
}

/* [1] becomes a jump into the hit list – no second source list showing the
   same entries again. */
function mitFussnoten(text){
  return esc(text).replace(/\[(\d+)\]/g, function(m, n){
    return kiQuellen[+n - 1]
      ? '<a href="#treffer-' + n + '" onclick="zeigeTreffer(' + n + ');return false;">' + m + '</a>'
      : m;
  });
}
function zitierte(text){
  var raus = [], m, re = /\[(\d+)\]/g;
  while((m = re.exec(text))) if(raus.indexOf(+m[1]) < 0) raus.push(+m[1]);
  return raus;
}
function markiereZitate(nummern){
  document.querySelectorAll('#results .hit').forEach(function(el2, i){
    el2.classList.toggle('zitiert', nummern.indexOf(i + 1) >= 0);
  });
}
function zeigeTreffer(n){
  var el2 = document.getElementById('treffer-' + n);
  if(el2) el2.scrollIntoView({behavior: 'smooth', block: 'center'});
}

/* ---------- Schedule / MCP ---------- */
function planAusFormular(){
  return {enabled: el('s-enabled').checked,
    interval_minutes: parseInt(el('s-interval').value, 10) || 60,
    outlook: el('s-outlook').checked, teams: el('s-teams').checked,
    onedrive: el('s-onedrive').checked, sharepoint: el('s-sharepoint').checked,
    sharepoint_pages: el('s-sharepoint_pages').checked,
    planner: el('s-planner').checked,
    index: el('s-index').checked, calendar: el('s-calendar').checked};
}

function planGeaendert(){
  var alt = (S && S.config && S.config.schedule) || {};
  var neu = planAusFormular();
  return Object.keys(neu).some(function(k){ return neu[k] !== alt[k]; });
}

function saveSchedule(){
  merke('flow.save', 'schedule');
  post('/api/schedule', planAusFormular()).then(refresh);
}
function toggleMcp(){
  merke('flow.mcp', S.mcp.running ? 'stop' : 'start');
  post('/api/mcp', {action: S.mcp.running ? 'stop' : 'start'}).then(function(r){
    if(!r.ok && r.message) alert(mtext(r.message));
    refresh();
  });
}

/* ---------- Updates ----------
   Only a note: nothing is downloaded, nothing replaced. Reported is solely
   the case "something newer exists" – no release, no network, or disabled
   are normal states and appear only in the settings. */
/* Three states, not two. "You are up to date" is wrong when the own version
   is HIGHER than anything published – then a self-built version runs here,
   and that deserves saying, not concealing. */
function zeigeUpdate(u){
  var banner = el('update-banner');
  var vorab = u.status === 'ok' && u.ahead;
  banner.classList.toggle('hide', !u.newer && !vorab);
  banner.classList.toggle('warn', vorab);
  if(u.newer){
    banner.innerHTML = esc(t('update.banner', {v: u.latest, current: u.current})) +
      ' <a href="' + esc(u.url || u.releases_url || '#') + '" target="_blank" rel="noopener">' +
      esc(t('update.open')) + '</a>';
  } else if(vorab){
    banner.textContent = t('update.ahead.banner', {v: u.current, latest: u.latest});
  }
  el('update-current').textContent = t('update.current', {v: u.current || '?'});
  el('update-state').textContent =
      u.status === 'ok' ? (u.newer ? t('update.available', {v: u.latest})
                         : u.ahead ? t('update.ahead', {v: u.latest})
                                   : t('update.uptodate'))
    : u.status === 'none' ? t('update.none')
    : u.status === 'error' ? t('update.error', {error: u.error || ''})
    : t('update.off');
  el('update-link').href = u.url || u.releases_url || '#';
}
function pruefeUpdate(){
  el('update-state').textContent = t('update.checking');
  post('/api/update-check').then(function(u){ zeigeUpdate(u); refresh(); });
}

/* ---------- Settings ---------- */
var SCHALTER = ['embed_images','cache_images','refresh_channels','skip_empty_chats',
                'include_hidden','calendar_reconstruct','mcp_enabled','mcp_autostart','update_check',
                'ollama_enabled','planner_attachments'];
var ZAHLEN   = ['workers','mirror_workers','index_batch','mcp_port','answer_sources','search_results',
                'onedrive_max_mb','sharepoint_max_mb',
                'sharepoint_pages_image_max_mb','semantic_min',
                'userflow_actions','runs_retention_months','log_retention_days'];
var TEXTE    = ['ollama','embed_model','chat_model',
                'folder_rules','onedrive_rules','calendar_rules',
                'sharepoint_types_include','sharepoint_types_exclude'];
var cfgGefuellt = false;

function fuelleEinstellungen(cfg){
  // Fill only once: the status arrives every 2.5 seconds, and re-setting
  // would replace a just-typed number or folder list under the fingers.
  if(cfgGefuellt) return;
  cfgGefuellt = true;
  SCHALTER.forEach(function(k){ el('c-'+k).checked = !!cfg[k]; });
  ZAHLEN.forEach(function(k){ el('c-'+k).value = cfg[k]; });
  TEXTE.forEach(function(k){ el('c-'+k).value = cfg[k] || ''; });
  el('c-notifications').value = cfg.notifications || 'errors';
  var kad = cfg.sync_cadence || {};
  fuelleUrlTabelle('sp-urls', cfg.sharepoint_urls, kad, 'sharepoint-url');
  fuelleUrlTabelle('pg-urls', cfg.sharepoint_pages_urls, kad, 'pages-url');
  fuelleUrlTabelle('pl-urls', cfg.planner_urls, kad, 'planner-url');
  el('c-cadence-onedrive').value = kad.onedrive || 'always';
  el('c-cadence-teams').value = kad.teams || 'always';
  el('c-skip_folders').value = (cfg.skip_folders || []).join('\n');
  el('c-filetype_hidden').value = (cfg.filetype_hidden || []).join(', ');
  el('c-analytics_skip').value = (cfg.analytics_skip || []).join('\n');
  // Two states that are not plain form fields: the Ollama switch grays out
  // half the card, the index kind lives in INDEX_SEMANTISCH.
  indexart(cfg.index_semantic !== false);
  ollamaSchalter();
  fuelleSprachen();
}
function leseKadenzen(){
  // Rebuilt from scratch: URL keys always mirror the current tables, so a
  // removed or edited row cannot leave a stale cadence behind.
  return {onedrive: el('c-cadence-onedrive').value,
          teams: el('c-cadence-teams').value};
}

function urlZeile(tabId, wert, kadenz){
  // One row per source URL: the address, its sync cadence, and "sync now".
  var tab = el(tabId);
  var zeile = document.createElement('div');
  zeile.className = 'zeile';
  zeile.onclick = function(){
    tab.querySelectorAll('.zeile').forEach(function(z){ z.classList.remove('an'); });
    zeile.classList.add('an');
  };
  var optionen = ['always', 'daily', 'weekly', 'monthly'];
  zeile.innerHTML =
    '<input type="text" placeholder="https://firma.sharepoint.com/sites/TeamX">' +
    '<select>' + optionen.map(function(o){
      return '<option value="' + o + '"' + (o === (kadenz || 'always') ? ' selected' : '') +
        '>' + esc(t('cadence.' + o)) + '</option>';
    }).join('') + '</select>' +
    '<button class="mini">' + esc(t('cadence.sync_now')) + '</button>';
  zeile.querySelector('input').value = wert || '';
  zeile.querySelector('select').onchange = speichereEinstellungen;
  zeile.querySelector('input').onchange = speichereEinstellungen;
  zeile.querySelector('button').onclick = function(ev){
    ev.stopPropagation();
    var url = zeile.querySelector('input').value.trim();
    if(!url) return;
    var lauf = tabId === 'sp-urls' ? {sharepoint: true}
      : tabId === 'pl-urls' ? {planner: true} : {sharepoint_pages: true};
    merke('flow.run', 'sync_now');
    post('/api/run', Object.assign({nur_einheit: url, label: 'job.export'}, lauf))
      .then(function(r){ if(!r.ok) alert(mtext(r.message)); refresh(); });
  };
  tab.appendChild(zeile);
  return zeile;
}

function urlZeileWeg(tabId){
  var an = el(tabId).querySelector('.zeile.an');
  if(an){ an.remove(); speichereEinstellungen(); }
}

function fuelleUrlTabelle(tabId, text, kadenzen, praefix){
  var tab = el(tabId);
  tab.innerHTML = '';
  tab.dataset.leer = t('cadence.units.none');
  String(text || '').split('\n').map(function(z){ return z.trim(); })
    .filter(Boolean).forEach(function(url){
      urlZeile(tabId, url, kadenzen[praefix + ':' + url]);
    });
}

function liesUrlTabelle(tabId, kadenzen, praefix){
  var urls = [];
  el(tabId).querySelectorAll('.zeile').forEach(function(z){
    var url = z.querySelector('input').value.trim();
    if(!url) return;
    urls.push(url);
    var wert = z.querySelector('select').value;
    if(wert !== 'always') kadenzen[praefix + ':' + url] = wert;
  });
  return urls.join('\n');
}

function speichereEinstellungen(){
  merke('flow.save', 'settings');
  // One save button for the whole tab – but only really post the schedule
  // when it changed: /api/schedule restarts the interval, and a settings
  // save must not keep postponing a run that is nearly due.
  if(planGeaendert()) saveSchedule();
  var body = {skip_folders: el('c-skip_folders').value,
              filetype_hidden: el('c-filetype_hidden').value,
              analytics_skip: el('c-analytics_skip').value,
              language: el('c-language').value,
              notifications: el('c-notifications').value,
              sync_cadence: leseKadenzen()};
  body.sharepoint_urls = liesUrlTabelle('sp-urls', body.sync_cadence,
                                        'sharepoint-url');
  body.sharepoint_pages_urls = liesUrlTabelle('pg-urls', body.sync_cadence,
                                              'pages-url');
  body.planner_urls = liesUrlTabelle('pl-urls', body.sync_cadence,
                                     'planner-url');
  var spracheVorher = (S.config && S.config.language) || 'auto';
  SCHALTER.forEach(function(k){ body[k] = el('c-'+k).checked; });
  ZAHLEN.forEach(function(k){ body[k] = parseInt(el('c-'+k).value, 10); });
  TEXTE.forEach(function(k){ body[k] = el('c-'+k).value.trim(); });
  body.index_semantic = INDEX_SEMANTISCH;
  post('/api/config', body).then(function(r){
    // The language is baked into the delivered page – switching needs a
    // rebuild, everything else takes effect immediately.
    if(r.config.language !== spracheVorher){ location.reload(); return; }
    cfgGefuellt = false;                 // play back saved (and clamped) values
    fuelleEinstellungen(r.config);
    // The hidden types affect the select list – it sits cached and would
    // otherwise stay stale until the next index run.
    typenJeQuelle = {};
    ladeTypen(el('f-source').value || 'all');
    var m = el('cfg-msg');
    m.className = 'small ok';
    m.textContent = t('settings.saved');
    setTimeout(function(){ m.textContent = ''; }, 4000);
    refresh();
  });
}
/* The folder tree is its own result, not a by-product of every export.
   This line says how old it is and what the rules make of it. */
function zeigeOrdnerstand(f, id){
  var kasten = el(id || 'folders-state');
  if(!kasten) return;
  if(!f.abgeglichen){ kasten.textContent = t('folders.none'); return; }
  var text = t(id ? 'folders.state.files' : 'folders.state',
                              {an: (f.ordner_gewaehlt || 0).toLocaleString(LOC),
                                 gesamt: (f.ordner_gesamt || 0).toLocaleString(LOC),
                                 mails: (f.mails_gewaehlt || 0).toLocaleString(LOC),
                                 when: fmt(f.abgeglichen)});
  if((f.neu || []).length) text += ' ' + t('folders.new', {n: f.neu.length});
  kasten.textContent = text;
}

/* Calendars count no events: how many sit in one, Graph does not say when
   listing. Hence its own line instead of zeigeOrdnerstand – in return it
   names the chosen calendars, which for a handful says more than any
   number. */
function zeigeKalenderstand(c){
  var kasten = el('cal-state');
  if(!kasten) return;
  if(!c || !c.abgeglichen){ kasten.textContent = t('settings.calendars.none'); return; }
  var text = t('settings.calendars.state',
               {an: (c.gewaehlt || 0).toLocaleString(LOC),
                gesamt: (c.gesamt || 0).toLocaleString(LOC),
                when: fmt(c.abgeglichen)});
  if((c.namen || []).length) text += ' – ' + c.namen.join(', ');
  if((c.neu || []).length) text += ' ' + t('folders.new', {n: c.neu.length});
  kasten.textContent = text;
}

var ABGLEICH = {
  onedrive: {msg: 'od-folders-msg', lauf: {sync_onedrive: true, label: 'job.folders'}},
  sharepoint: {msg: 'sp-msg', lauf: {sync_sharepoint: true, label: 'job.folders'},
               save: function(){ return speichereSharepointFelder(); }},
  calendar: {msg: 'cal-msg', lauf: {sync_calendars: true, label: 'job.calendars'}},
  outlook:  {msg: 'folders-msg', lauf: {sync_folders: true, label: 'job.folders'}}
};

function speichereSharepointFelder(){
  // The buttons must act on what the form shows, not on the last save –
  // otherwise an edited URL list feels ignored until someone hits Save.
  var kad = leseKadenzen();
  return post('/api/config', {
    sharepoint_urls: liesUrlTabelle('sp-urls', kad, 'sharepoint-url'),
    sharepoint_pages_urls: liesUrlTabelle('pg-urls', kad, 'pages-url'),
    sync_cadence: kad,
    sharepoint_types_include: el('c-sharepoint_types_include').value,
    sharepoint_types_exclude: el('c-sharepoint_types_exclude').value,
    sharepoint_max_mb: parseInt(el('c-sharepoint_max_mb').value, 10) || 0});
}
function sharepointVorschau(){
  // The check run enumerates without downloading; the merged report lands in
  // Analytics, the one-line summary right here next to the button.
  el('sp-msg').textContent = t('sharepoint.preview.running');
  speichereSharepointFelder().then(function(){
    return post('/api/run', {check_sharepoint: true, label: 'job.preview'});
  }).then(function(r){
    if(!r.ok){ el('sp-msg').textContent = mtext(r.message); return; }
    var timer = setInterval(function(){
      if(S && S.jobs && !S.jobs.busy){
        clearInterval(timer);
        api('/api/sharepoint-report').then(function(r){
          var b = r.bericht;
          el('sp-msg').textContent = b && (b.erwartet || b.ausgelassen)
            ? t('sharepoint.preview.result',
                {n: zahl(b.erwartet), mb: zahl(Math.round((b.bytes || 0) / 1048576)),
                 skipped: zahl(b.ausgelassen || 0)})
            : t('sharepoint.preview.empty');
          malSharepointTypen(b);
        });
      }
    }, 1500);
  });
}
function zeigeSharepointTypen(){
  api('/api/sharepoint-report').then(function(r){
    malSharepointTypen(r.bericht);
  }).catch(function(){});
}
function malSharepointTypen(b){
  var kasten = el('sp-typen');
  if(!b || !(b.typen || []).length){
    kasten.textContent = t('sharepoint.types.none'); return;
  }
  kasten.innerHTML = (b.typen || []).slice(0, 30).map(function(z){
    return '<span style="display:inline-block;margin:2px 10px 2px 0">' +
      '<code>' + esc(z.ext || '·') + '</code> ' + esc(zahl(z.n)) + ' · ' +
      esc(bytes(z.bytes)) + '</span>';
  }).join('');
}
function gleicheOrdnerAb(quelle){
  var wahl = ABGLEICH[quelle] || ABGLEICH.outlook;
  var kasten = wahl.msg;
  el(kasten).textContent = t('folders.syncing');
  // Only SharePoint saves its form first – the other sources start
  // synchronously, their rules travel inside the request itself.
  var start = wahl.save
    ? function(){ return wahl.save().then(function(){ return post('/api/run', wahl.lauf); }); }
    : function(){ return post('/api/run', wahl.lauf); };
  start().then(function(r){
    if(!r.ok){ el(kasten).textContent = mtext(r.message); return; }
    wennLaufFertig(function(){ el(kasten).textContent = ''; });
  });
}

function setzeAblage(koerper, feldId, msgId){
  post('/api/data-dir', koerper).then(function(r){
    var kasten = el(msgId);
    if(!r.ok){ kasten.className = 'small err'; kasten.textContent = mtext(r.message); return; }
    el(feldId).value = koerper.path !== undefined ? r.path : r.index;
    kasten.className = 'small muted';
    kasten.textContent = t(r.restart ? 'settings.datadir.restart' : 'settings.datadir.same');
  });
}
function setzeDatenordner(pfad){
  setzeAblage({path: pfad !== undefined ? pfad : el('c-data-dir').value.trim()},
              'c-data-dir', 'datadir-msg');
}
function datenordnerZurueck(){ setzeDatenordner(''); }
function setzeIndexordner(pfad){
  setzeAblage({index: pfad !== undefined ? pfad : el('c-index-dir').value.trim()},
              'c-index-dir', 'indexdir-msg');
}
function indexordnerZurueck(){ setzeIndexordner(''); }

function ordnerZuruecksetzen(){
  el('c-skip_folders').value = (S.skip_folders_default || []).join('\n');
}
function typenZuruecksetzen(){
  el('c-filetype_hidden').value = (S.filetype_hidden_default || []).join(', ');
  speichereEinstellungen();
}

/* ---------- Export list ----------
   The rules are powerful; computing their result in one's head is not:
   "- E-Mail/Kunden/**" and a "+" on a subfolder two lines later decide over
   four hundred folders. Whoever cannot see that configures blindly. So the
   same evaluation as in the export runs here – only as a list instead of a
   run, and with what currently stands in the fields, not with what was
   last saved. */
var planDaten = null, planQuelle = 'outlook';

function zeigeExportliste(quelle){
  planDaten = null;
  planQuelle = quelle || 'outlook';
  planFenster();
  post('/api/folder-plan', planQuelle === 'sharepoint'
      ? {quelle: 'sharepoint'}
      : planQuelle === 'onedrive'
      ? {quelle: 'onedrive', onedrive_rules: el('c-onedrive_rules').value}
      : planQuelle === 'calendar'
      ? {quelle: 'calendar', calendar_rules: el('c-calendar_rules').value}
      : {folder_rules: el('c-folder_rules').value,
         skip_folders: el('c-skip_folders').value})
    .then(function(p){
      planDaten = p;
      if(wizardOffen === 'plan') planFenster();
    });
}

function planFenster(){
  var p = planDaten, koerper;
  if(!p){
    koerper = '<p class="small muted">' + esc(t('plan.loading')) + '</p>';
  } else if(!p.ok){
    koerper = '<div class="banner warn">' + esc(t('folders.none')) + '</div>';
  } else {
    koerper = '<p class="small muted">' + esc(t('plan.stand', {when: fmt(p.abgeglichen)})) + '</p>' +
      '<input type="text" id="plan-filter" oninput="planListen()" ' +
        'placeholder="' + esc(t('plan.filter')) + '" style="width:100%;margin:12px 0 2px">' +
      '<div id="plan-listen"></div>';
  }
  oeffneEigenes('plan', modalKopf(t('plan.title'), 'plan') + koerper +
    modalFuss({text: t(planQuelle === 'calendar' ? 'settings.calendars.sync' : 'folders.sync'),
               tun: 'planAbgleichen(&quot;' + planQuelle + '&quot;)'}));
  if(p && p.ok) planListen();
}

/* First the path, then the number, then the reason – in the order one asks.
   For skipped folders, what nevertheless already sits in the archive stands
   in between: "skipped" does not mean "empty", and whoever confuses the two
   will later search for mails that have long been there. */
function planZeile(e, zahl, mitArchiv){
  return '<li><span class="pfad">' + esc(e.pfad) + '</span>' +
    '<span class="zahl">' + (e[zahl] || 0).toLocaleString(LOC) + '</span>' +
    (mitArchiv && e.archiv
      ? '<span class="regel">' + esc(t(planQuelle === 'onedrive' ? 'plan.here' : 'plan.inarchive',
                                       {n: e.archiv.toLocaleString(LOC)})) + '</span>'
      : '') +
    (e.regel ? '<span class="regel">' + esc(e.regel) + '</span>' : '') + '</li>';
}

function planListen(){
  var p = planDaten;
  if(!p || !p.ok || !document.getElementById('plan-listen')) return;
  var f = (el('plan-filter').value || '').trim().toLowerCase();
  function gruppe(schluessel, liste, zahl, mails, punkt, mitArchiv){
    var zeilen = liste.filter(function(e){
      return !f || e.pfad.toLowerCase().indexOf(f) >= 0; });
    return '<details class="plangruppe" open><summary><span class="dot ' + punkt + '"></span>' +
      esc(t(schluessel, {n: liste.length.toLocaleString(LOC),
                         mails: mails.toLocaleString(LOC)})) +
      (f ? ' <span class="small muted">' + esc(t('plan.shown', {n: zeilen.length})) + '</span>' : '') +
      '</summary>' +
      (zeilen.length
        ? '<ul class="planliste">' + zeilen.map(function(e){
            return planZeile(e, zahl, mitArchiv); }).join('') + '</ul>'
        : '<p class="small muted">' + esc(t('plan.nothing')) + '</p>') + '</details>';
  }
  // For the mirror these are files, not mails – and what lies here is not
  // archiving but the remains of a deleted folder. Written out instead of
  // assembled, so the comparison against the language files finds the
  // keys.
  var dat = planQuelle === 'onedrive', kal = planQuelle === 'calendar';
  // For calendars there is nothing to compare: how many events are inside,
  // Graph does not reveal when listing. So what already lies on disk is
  // counted – the only number honestly available here.
  function ablage(liste){
    return liste.reduce(function(a, e){ return a + (e.archiv || 0); }, 0);
  }
  el('plan-listen').innerHTML = kal
    ? gruppe('plan.an.cal',  p.an,  'archiv', ablage(p.an),  'ok',   false) +
      gruppe('plan.aus.cal', p.aus, 'archiv', ablage(p.aus), 'warn', false) +
      gruppe('plan.weg.cal', p.weg, 'archiv', p.mails_weg,   'err',  false)
    : gruppe(dat ? 'plan.an.files'  : 'plan.an',  p.an,  'elemente', p.mails_an,  'ok',   false) +
      gruppe(dat ? 'plan.aus.files' : 'plan.aus', p.aus, 'elemente', p.mails_aus, 'warn', true) +
      gruppe(dat ? 'plan.weg.files' : 'plan.weg', p.weg, 'archiv',   p.mails_weg, 'err',  false);
}

function planAbgleichen(quelle){
  closeWizard('plan');
  gleicheOrdnerAb(quelle);
}

/* ---------- Wizards ---------- */
/* Fingerprint of what the wizard currently shows. If it does not change,
   nothing is redrawn – the status arrives every 2.5 seconds, and a freshly
   set innerHTML would otherwise throw away half-finished input. If it does
   change (model pulled, token saved), a redraw is required, or the wizard
   claims things that are long done. The token's remaining lifetime is
   deliberately not part of it: it changes every minute without anything
   essential changing in the text. */
function wizardKennung(kind){
  if(kind === 'ollama'){
    var o = S.ollama || {};
    return ['ollama', o.running, o.has_model, o.model].join('|');
  }
  var tk = S.token || {}, au = S.auth || {}, dev = au.device || {};
  return ['token', tk.present, tk.valid, tk.expired, tk.account,
          (tk.missing || []).join(','), (S.scopes_needed || []).join(','),
          au.mode, au.signed_in, au.account, au.own_registration,
          dev.code, dev.done, dev.ok].join('|');
}
/* ---------- Keyboard in the wizard ----------
   A modal window seizes the page; whoever uses no mouse must still get in,
   around and out again. ESC closes, Tab stays inside (otherwise focus
   wanders invisibly behind the overlay), and on close it returns to where
   it came from. */
var fokusVorher = null;

function fokussierbare(){
  return [].slice.call(el('modal').querySelectorAll(
    'button, [href], textarea, input, select, summary, [tabindex]:not([tabindex="-1"])'));
}
function modalTaste(e){
  if(!wizardOffen) return;
  if(e.key === 'Escape'){ e.preventDefault(); closeWizard(wizardOffen); return; }
  // Ctrl/Cmd+Enter in the text field: save without tabbing to the button.
  if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){
    var act = el('modal').querySelector('button.act');
    if(act){ e.preventDefault(); act.click(); }
    return;
  }
  if(e.key !== 'Tab') return;
  var liste = fokussierbare();
  if(!liste.length) return;
  var erster = liste[0], letzter = liste[liste.length - 1];
  if(e.shiftKey && document.activeElement === erster){ e.preventDefault(); letzter.focus(); }
  else if(!e.shiftKey && document.activeElement === letzter){ e.preventDefault(); erster.focus(); }
}
document.addEventListener('keydown', modalTaste);

/* A window the server never opens on its own: it has no fingerprint the
   status could compare every 2.5 seconds, and must not be wiped away when
   the wizard redraws (see refresh). */
var WIZARDS = {token: 1, ollama: 1};

function oeffneEigenes(kind, html){
  var warOffen = !!wizardOffen;
  if(!warOffen) fokusVorher = document.activeElement;
  el('modal').className = 'modal breit';
  el('modal').innerHTML = html;
  el('overlay').classList.add('on');
  wizardOffen = kind;
  wizardStand = null;
  if(!warOffen){
    var ziel = el('modal').querySelector('input, button.act');
    if(ziel && ziel.focus) ziel.focus();
  }
}

function openWizard(kind, neuZeichnen){
  if(!neuZeichnen) merke('flow.wizard', kind);
  var kennung = wizardKennung(kind);
  if(wizardOffen === kind && wizardStand === kennung && !neuZeichnen) return;
  var feld = document.getElementById('tok');       // rescue what was already pasted
  var eingabe = feld ? feld.value : '';
  var warOffen = !!wizardOffen;
  if(!warOffen) fokusVorher = document.activeElement;
  el('modal').className = 'modal';
  el('modal').innerHTML = kind === 'ollama' ? ollamaWizard() : tokenWizard();
  var neu = document.getElementById('tok');
  if(neu && eingabe) neu.value = eingabe;
  el('overlay').classList.add('on');
  wizardOffen = kind;
  wizardStand = kennung;
  // Jump into the dialog on open – but not on every redraw, or it would
  // tear the focus right out of the text field.
  if(!warOffen){
    var ziel = document.getElementById('tok') || el('modal').querySelector('button.act');
    if(ziel && ziel.focus) ziel.focus();
  }
}
function closeWizard(kind){
  dismissed[kind] = true;
  el('overlay').classList.remove('on');
  wizardOffen = null;
  wizardStand = null;
  if(fokusVorher && fokusVorher.focus) fokusVorher.focus();
  fokusVorher = null;
  // Only wizards are to be reported as "seen" – a self-opened window was
  // never demanded by the server and must not reset anything on it either.
  if(WIZARDS[kind]) post('/api/wizard-seen');
}
/* Frame for all wizards. The same rule everywhere: cross at the top right
   to close, the action at the bottom left, the fallback next to it – so no
   window makes "close" its primary button while the real action sits pale
   beside it. */
function modalKopf(titel, kind){
  var zu = esc(t('wizard.close'));
  return '<div class="modal-kopf"><h2>' + esc(titel) + '</h2>' +
    '<button class="modal-zu" title="' + zu + '" aria-label="' + zu + '" ' +
    'onclick="closeWizard(&quot;' + kind + '&quot;)">&times;</button></div>';
}
/* The secondary button may be absent. A "later" that does nothing the cross
   above does not is no second option – just the same exit twice, and the
   eye has to check it twice. */
function modalFuss(primaer, sekundaer, anhang){
  return '<div class="row modal-fuss">' +
    '<button class="act" onclick="' + primaer.tun + '">' + esc(primaer.text) + '</button>' +
    (sekundaer ? '<button class="ghost" onclick="' + sekundaer.tun + '">' +
                 esc(sekundaer.text) + '</button>' : '') +
    (anhang || '') + '</div>';
}

function scopeListe(){
  var q = S.scope_queries || {};
  return '<ul style="margin:6px 0 0;padding-left:18px">' + (S.scopes_needed || []).map(function(x){
    return '<li style="margin-bottom:3px"><code>' + esc(x) + '</code>' +
      (q[x] ? '<br><span class="small muted">' + esc(t('wizard.token.scopes.query')) +
              ' </span><code class="small">' + esc(q[x]) + '</code>' : '') + '</li>';
  }).join('') + '</ul>';
}

/* The permissions are the most technical part of the dialog – names like
   Contacts.Read plus Graph URLs. Usually they were granted long ago and
   then only stand in the way. Collapsed they stay reachable; expanded
   exactly when they are actually missing and thus the topic. */
function rechteBlock(offen){
  return '<details class="rechte"' + (offen ? ' open' : '') + '>' +
    '<summary>' + esc(t('wizard.token.scopes.title')) + '</summary>' +
    '<p class="small muted">' + t('wizard.token.scopes.intro') + '</p>' +
    scopeListe() +
    '<p class="small muted">' + esc(t('wizard.token.scopes.note')) + '</p></details>';
}

function modusWahl(){
  /* Two paths, one of them the default. The difference that matters stands
     right next to it – not in a help page nobody opens. */
  var jetzt = (S.auth && S.auth.mode) || 'token';
  function karte(wert, titel, hinweis){
    return '<label class="wahl' + (jetzt === wert ? ' on' : '') + '">' +
      '<input type="radio" name="authmode" value="' + wert + '"' +
      (jetzt === wert ? ' checked' : '') + ' onchange="setzeModus(\'' + wert + '\')">' +
      '<span><strong>' + esc(t(titel)) + '</strong>' +
      '<span class="small muted">' + esc(t(hinweis)) + '</span></span></label>';
  }
  return '<div class="wahlreihe">' +
    karte('token', 'wizard.auth.token', 'wizard.auth.token.hint') +
    karte('login', 'wizard.auth.login', 'wizard.auth.login.hint') + '</div>';
}

function eigeneRegistrierung(){
  var au = S.auth || {};
  return '<details class="rechte"' + (au.own_registration ? ' open' : '') + '>' +
    '<summary>' + esc(t('wizard.login.own.title')) + '</summary>' +
    '<p class="small muted">' + t('wizard.login.own.intro') + '</p>' +
    '<div class="row"><label class="small">' + esc(t('wizard.login.own.client')) +
    ' <input type="text" id="au-client" style="width:320px" value="' +
    esc(au.own_registration ? (au.client_id || '') : '') + '" placeholder="' +
    esc(au.default_client_id || '') + '"></label>' +
    '<label class="small">' + esc(t('wizard.login.own.tenant')) +
    ' <input type="text" id="au-tenant" style="width:220px" value="' +
    esc(au.own_registration ? (au.tenant || '') : '') + '" placeholder="organizations"></label>' +
    '<button class="mini" onclick="speichereRegistrierung()">' +
    esc(t('wizard.login.own.save')) + '</button></div></details>';
}

function loginTeil(){
  var au = S.auth || {}, dev = au.device;
  var kopf;
  if(au.signed_in)
    kopf = banner('', '<span class="ok">✓</span> ' +
      t(au.account ? 'wizard.login.state.in' : 'wizard.login.state.in.plain',
        {who: esc(au.account || '')}));
  else
    kopf = banner('warn', esc(t('wizard.login.state.out')));

  var mitte = '';
  if(dev && !dev.done){
    // The code is the only thing that matters now – large and copyable.
    mitte = '<div class="geraetecode">' +
      '<p>' + t('wizard.login.code.intro', {url: esc(dev.url)}) + '</p>' +
      '<code class="code-gross">' + esc(dev.code) + '</code>' +
      '<p class="small muted">' + esc(t('wizard.login.waiting')) + '</p></div>';
  } else if(dev && dev.done && !dev.ok){
    mitte = banner('err', esc(t('wizard.login.failed', {detail: dev.error || ''})));
  }

  var primaer = au.signed_in
    ? {text: t('wizard.login.again'), tun: 'starteLogin()'}
    : {text: t('wizard.login.start'), tun: 'starteLogin()'};
  var sekundaer = au.signed_in
    ? {text: t('wizard.login.logout'), tun: 'abmelden()'} : null;

  return '<p class="muted small">' + esc(t('wizard.login.intro')) + '</p>' +
    kopf + mitte + eigeneRegistrierung() + modalFuss(primaer, sekundaer);
}

function schluesselTeil(){
  var tk = S.token, head, fehlen = !!(tk.missing && tk.missing.length);
  if(!tk.present) head = banner('err', t('wizard.token.none'));
  else if(tk.expired) head = banner('err', t('wizard.token.expired'));
  else if(fehlen)
    head = banner('warn', t('wizard.token.missing', {list: esc(tk.missing.join(', '))}));
  else {
    // Four whole sentences instead of assembled fragments – see the language files.
    var hatWer = !!tk.account, hatZeit = tk.expires_in_minutes != null;
    var k = hatWer && hatZeit ? 'wizard.token.ok'
          : hatWer ? 'wizard.token.ok.unknown'
          : hatZeit ? 'wizard.token.ok.nowho' : 'wizard.token.ok.plain';
    head = banner('', '<span class="ok">✓</span> ' + t(k, {who: esc(tk.account || ''),
                                                          rest: restzeit(tk.expires_in_minutes)}));
  }
  return '<p class="muted small">' + esc(t('wizard.token.intro')) + '</p>' + head +
    rechteBlock(fehlen) +
    '<ol><li>' + t('wizard.token.step1', {url: esc(S.graph_explorer)}) + '</li>' +
    '<li>' + t('wizard.token.step2') + '</li>' +
    '<li>' + esc(t('wizard.token.step3')) + '</li></ol>' +
    '<textarea id="tok" placeholder="eyJ0eXAiOiJKV1QiLCJub25jZSI6…"></textarea>' +
    modalFuss({text: t('wizard.token.save'), tun: 'saveToken()'}, null,
              '<span class="small muted" id="tok-msg"></span>');
}

function tokenWizard(){
  var login = (S.auth && S.auth.mode) === 'login';
  return modalKopf(t('wizard.token.title'), 'token') +
    modusWahl() +
    (login ? loginTeil() : schluesselTeil()) +
    '<p class="small muted" style="margin-top:14px">' +
    t(login ? 'wizard.login.privacy' : 'wizard.token.privacy') + '</p>';
}

function setzeModus(wert){
  post('/api/config', {auth_mode: wert}).then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function starteLogin(){
  post('/api/login').then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function abmelden(){
  post('/api/logout').then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}
function speichereRegistrierung(){
  post('/api/config', {client_id: el('au-client').value.trim(),
                       tenant: el('au-tenant').value.trim()}).then(function(){
    refresh().then(function(){ openWizard('token', true); });
  });
}

function banner(art, html){
  return '<div class="banner' + (art ? ' ' + art : '') + '">' + html + '</div>';
}
function saveToken(){
  post('/api/token', {token: el('tok').value}).then(function(r){
    el('tok-msg').textContent = mtext(r.message);
    el('tok-msg').className = 'small ' + (r.ok ? 'ok' : 'err');
    if(r.ok){ dismissed = {};
              setTimeout(function(){ el('overlay').classList.remove('on');
                                     wizardOffen = null; wizardStand = null; }, 1200); }
    refresh();
  });
}
function ollamaWizard(){
  var o = S.ollama, h = S.ollama_hint;

  // Everything there – that can happen while the wizard stands open and an
  // "ollama pull" runs through on the side. Then confirm instead of nagging
  // on.
  if(o.running && o.has_model){
    // Re-indexing is the action this window opens for in the first place –
    // so it leads, rather than sitting pale next to "close".
    return modalKopf(t('wizard.ollama.ready.title'), 'ollama') +
      banner('', '<span class="ok">✓</span> ' + t('wizard.ollama.ready', {model: esc(o.model)})) +
      modalFuss({text: t('wizard.ollama.reindex'),
                 tun: 'closeWizard(&quot;ollama&quot;); run({index:true}, t(&quot;job.index&quot;))'},
                null);
  }

  var head = banner('warn', o.running ? t('wizard.ollama.nomodel', {model: esc(o.model)})
                                      : esc(t('wizard.ollama.off')));
  var steps = o.running
    ? [t('wizard.ollama.pull', {model: esc(o.model)}), esc(t('wizard.ollama.wait'))]
    : (h.steps || []).map(function(k){ return t(k, {url: esc(h.url || '')}); });
  return modalKopf(t('wizard.ollama.title'), 'ollama') +
    '<p class="muted small">' + esc(t('wizard.ollama.intro')) + '</p>' + head +
    '<ol>' + steps.map(function(x){ return '<li>' + x + '</li>'; }).join('') + '</ol>' +
    (h.pkg && !o.running ? '<p class="small muted">' + esc(t('wizard.ollama.pkg')) +
                           '</p><pre>' + esc(h.pkg) + '</pre>' : '') +
    // No "later" here: the cross above does the same, and the way out
    // without Ollama is the decision actually at hand.
    modalFuss({text: t('wizard.ollama.recheck'), tun: 'recheckOllama()'},
              {text: t('wizard.ollama.without'),
               tun: 'closeWizard(&quot;ollama&quot;); run({index:true, embeddings:false}, ' +
                    't(&quot;job.index.lexical&quot;))'}) +
    '<p class="small muted" style="margin-top:14px">' + esc(t('wizard.ollama.without.note')) + '</p>';
}
function recheckOllama(){
  post('/api/ollama-recheck').then(function(){ refresh().then(function(){ openWizard('ollama', true); }); });
}

/* ---------- Quit ----------
   The app has no window and is not in the dock – without this button only
   the activity monitor would remain. The MCP server goes down with it; a
   running job is cancelled, but everything already exported is kept. */
var beendet = false;
function beenden(){
  var laeuft = S && S.jobs && S.jobs.busy;
  if(!confirm(t(laeuft ? 'quit.confirm.busy' : 'quit.confirm'))) return;
  beendet = true;
  post('/api/quit').catch(function(){});   // the reply may never arrive
  document.querySelector('main').innerHTML =
    '<div class="card"><p>' + esc(t('quit.done')) + '</p></div>';
  document.querySelector('nav').classList.add('hide');
  // Pills and log too: they would otherwise show frozen states of an app
  // that no longer runs – and their buttons would call a dead API.
  el('pills').classList.add('hide');
  el('protokoll').classList.add('hide');
  protokollPlatz();
}

/* ---------- Loop ---------- */
function refresh(){
  if(beendet) return Promise.resolve();
  return api('/api/status').then(renderStatus);
}
stelleProtokollHer();
refresh();
setInterval(refresh, 2500);
setInterval(pullLog, 1000);
</script>
</body>
</html>
"""
