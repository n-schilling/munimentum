# What comes next

A short list of what is planned, so nobody has to guess later whether
something was forgotten or left out on purpose. How things get built lives in
the code, not here.

## Smaller

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
