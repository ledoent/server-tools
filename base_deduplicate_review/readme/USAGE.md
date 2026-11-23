1. Go to *Data Cleaning > Configuration > Rules > Deduplication* and create a
   rule: pick a model, optionally a filter, and one or more fields with how they
   should match. Click *Find Duplicates*, or wait for the daily job.
2. Go to *Data Cleaning > Duplicates*. Open a group:
   - *Keep This* chooses the record that survives;
   - *Excluded* leaves a member out;
   - *Count Usage* shows how much references each member.
3. A reviewer clicks *Validate*, or *Not Duplicates*. A manager then clicks
   *Merge*.
4. *Data Cleaning > Merged Records* lists every merged-away record, what it
   merged into, and the identifiers that now resolve there.

For importers and other modules:

- `deduplicate.group.deduplicate_propose(model, proposals)` creates or refreshes
  groups keyed on a stable `external_key`, and never changes one a person has
  acted on.
- `deduplicate.group.deduplicate_decisions(model, keys)` reads the outcome.
- `deduplicate.alias.resolve_identifiers(model, field, values=None, prefix=None)`
  and `resolve_ids(model, ids)` map merged records to the record they became.
