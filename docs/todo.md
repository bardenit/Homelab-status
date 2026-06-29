# Look-at-later

Running list of things to revisit. Not blocking; captured so they don't get lost.

- [ ] **PBS `gc_age_h` is null for both datastores.** `/api/status` returns
  `gc_age_h: null` for MainBackup and lil_Backup. Either no garbage-collection
  task has run yet, or the `DatastoreAudit` token can't read the GC task log on
  node `localhost` (the `/nodes/{node}/tasks?typefilter=garbage_collection`
  call in `fetch_pbs`). Degrades gracefully and isn't displayed yet (see
  CLAUDE.md backlog item 2), so low priority. Check: run a GC in PBS, re-poll;
  if still null, it's a token/permission or node-name (`localhost`) issue.
