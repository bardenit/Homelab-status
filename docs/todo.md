# Look-at-later

Running list of things to revisit. Not blocking; captured so they don't get lost.

- [x] **PBS `gc_age_h` was null** (resolved 2026-07-01). Root cause was
  permissions, not the node name (`localhost` is valid for PBS): `DatastoreAudit`
  reads datastore *usage* but PBS hides GC/task logs and `/nodes` from non-owners
  without **`Sys.Audit`**. Fixed by granting the read-only **`Audit`** role at `/`
  to BOTH the user (`APIGuy@pbs`) and the token (`APIGuy@pbs!LabMonitor`) — token
  privilege-separation means effective perms are the user∩token intersection.
  `fetch_pbs` also rewritten to query GC per-datastore (`?store=`) + discover the
  node name + log failures. GC hours now display on the BACKUPS page.
