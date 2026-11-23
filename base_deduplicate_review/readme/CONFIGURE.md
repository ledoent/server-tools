Give users one of the *Deduplication* groups:

- **Reviewer**: sees duplicate groups, chooses the record to keep, validates
  or discards.
- **Manager**: also merges, configures rules, and proposes groups through the
  API.

A model whose merge has application-specific side effects can declare a
dedicated strategy by overriding `_deduplicate_merge_records` and setting
`_deduplicate_has_strategy = True`. Contacts and CRM leads are merged that way
by their own modules; without them, a generic merge of those models is refused.
A model can also refuse merging altogether with `_disable_data_merge = True`.
