# What comes next

A short list of what is planned, so nobody has to guess later whether
something was forgotten or left out on purpose. How things get built lives in
the code, not here.

## Next: one state.db per export folder — for the older exports

The two SharePoint exports already keep their state in one SQLite per folder
(state_db.py); Outlook, Teams and OneDrive still use the loose files their
existing archives are built on. Extending the pattern means a careful
migration of exactly the resume data — its own release.

## Smaller

* **Split the data directory.** Bulk data and index have different needs —
  the `.eml` files may live on a slow disk, the index must not. Separate
  paths already work, but nothing explains them.
* **A step registry.** Every export action is hand-threaded through four
  layers (API handler, launch, build_steps, run record); a declarative table
  keyed by step name would collapse them. Best done together with the app.py
  split below.
* **Split app.py.** Half the file is the interface as one embedded string,
  the rest is Python (config, runs, routes, analytics). A rework of its own —
  many tests check the page as a string, and bundling depends on it.

## Later, maybe

* **Searching file contents.** The index knows file names, not what is in
  them. Deliberately parked: extraction is a heavy step, and whether the
  archive needs it at all is not settled yet.
