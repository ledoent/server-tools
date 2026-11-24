Find duplicate records with rules, have a person review them, and merge them
without losing what pointed at them.

- **Rules** name a model, an optional filter, and the fields that make two
  records the same. Fields compare exactly, ignoring case, accents and
  spacing, as e-mail addresses, or as phone numbers. A daily job re-runs every
  rule. A run follows the data and never re-asks a question somebody has
  already decided.
- **Groups** come in two kinds:
  - *Duplicates*: every member is the same thing, and the others merge into
    the one kept.
  - *Match to one of*: one source record and the records it might be; only
    the source merges.

  Other modules and imports can propose groups through
  `deduplicate.group.deduplicate_propose`.
- **Review** happens before any merge. A reviewer picks the record to keep,
  excludes members that do not belong, sees how much references each member,
  and validates or discards the group. A manager merges it. With *Require
  validation* (the default), no merge happens on one person's decision alone.
- **Merging** moves every reference to the record kept, safely:
  - rows that would duplicate one the record kept already has are dropped,
    under unique constraints and unique indexes alike, partial ones included;
    everything else moves;
  - a one-to-one conflict refuses the merge instead of guessing;
  - references that are not foreign keys move too: messages, attachments,
    activities, followers and every other stored *Many2oneReference* or
    *Reference* field, plus company-dependent values;
  - stored computed fields are recomputed;
  - hierarchies keep their parent paths;
  - the record kept inherits values it lacked.
- **Merged records** stay resolvable. Their unique identifiers (by default,
  uncopied Char fields under a single-column unique index) are kept with the
  record they merged into, so an import or synchronisation keyed on them can
  resolve them instead of creating them again.
