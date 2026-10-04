# What comes next

A short list of what is planned, so nobody has to guess later whether
something was forgotten or left out on purpose. How things get built lives in
the code, not here.

## Later, maybe

* **Searching across profiles.** Since 10.0 every profile is one archive
  with one account, one index, and its own MCP snippet. A search that
  spans two of them would need a merged view over two indexes and a
  header that says which hit came from where. Parked: keeping the
  archives apart is what a profile is for, and Claude can hold two
  servers.
* **Searching file contents.** The index knows file names, not what is in
  them. Deliberately parked: extraction is a heavy step, and whether the
  archive needs it at all is not settled yet.
* **A new folder address without a full walk.** Adding a folder address to
  a SharePoint library changes the library's selection, and the next run
  reads the whole library once to find the folder's files – minutes for
  a large library, for one folder. Listing only the new subtree and
  keeping the inventory would make it seconds; a file that already lies
  there with the same bytes is not written again even today.
* **Steps of a run in parallel.** The mirrors and the index have other
  budgets than the mailbox and Teams, so they could overlap. Parked: the
  run window, the log order and the disk would all have to learn it, and
  since 9.0 every source asks only for what changed, so the runs are short
  anyway.
