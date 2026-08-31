# What comes next

A short list of what is planned, so nobody has to guess later whether
something was forgotten or left out on purpose. How things get built lives in
the code, not here.

## Next: searching file contents

Today the index knows **names**: the file-type filter finds mails with a PDF,
`Vertrag_Musterkunde.pdf` finds the one, and the OneDrive and SharePoint
mirrors work the same way. What is missing is the **content** — a contract
sits in the archive, its text invisible.

All sources share the problem, so it becomes one step: attachments from the
`.eml` files and mirrored drive files go through the same extraction. The
ground is prepared — `text` currently carries the path and is exactly the
field an extraction fills later. An existing index stays valid, it only gets
richer.

Three things are settled:

* **A separate, switchable step.** On a real archive it takes about an hour,
  once — not something to run with every export.
* **Cache by content hash, not path.** The same file in twelve mails costs
  one extraction, and all sources share the cache.
* **Not everything.** Roughly 40 % of attachments are images, mostly
  signature logos. A type filter is part of the deal.

Open question: the tooling.
[markitdown](https://github.com/microsoft/markitdown) covers completeness
(tables, notes, headers) but drags in a dependency tree that would multiply
the 27 MB bundle. The lean alternative of individual libraries is much
smaller, but completeness becomes our job. To be decided against a real
bundle, not at the desk.

## Smaller

* **Split the data directory.** Bulk data and index have different needs —
  the `.eml` files may live on a slow disk, the index must not. Separate
  paths already work, but nothing explains them.
* **One state.db per export folder — for the older exports.** The two
  SharePoint exports already keep their state in one SQLite per folder
  (state_db.py); Outlook, Teams and OneDrive still use the loose files
  their existing archives are built on. Extending the pattern means a
  delicate migration of exactly the resume data — its own release, with a
  dual-read transition.
* **A step registry.** Every export action is hand-threaded through four
  layers (API handler, launch, build_steps, run record); a declarative table
  keyed by step name would collapse them. Best done together with the app.py
  split below.
* **Split app.py.** Half the file is the interface as one embedded string,
  the rest is Python (config, runs, routes, analytics). A rework of its own —
  many tests check the page as a string, and bundling depends on it.
