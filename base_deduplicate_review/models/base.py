# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The merge every deduplication strategy builds on.

A merge moves every reference from the merged-away records (the sources) to
the record that survives (the master), then removes the sources. Doing that
safely for any model means handling four things the obvious UPDATE does not:

* A unique key on the referencing table. Moving a source's row onto the
  master can collide with a row the master already has: two followers of the
  same document, two notifications of the same message, the same pair in a
  many2many table. Only the rows that genuinely duplicate the master's are
  removed; every other row moves. Keys are read from ``pg_index``, so a unique
  index (not only a unique constraint, and including partial ones) is seen.
* A one-to-one key, where the master and a source both own a row. There is no
  safe automatic answer, so the merge is refused.
* References that are not foreign keys: every stored ``Many2oneReference``
  (messages, attachments, activities, followers, ratings, mailing traces,
  calendar events, ...), ``Reference`` fields, and company-dependent
  many2one values held in jsonb.
* Values derived from the moved references: stored computed fields are marked
  modified and recomputed, and parent-store hierarchies are updated through
  the ORM so ``parent_path`` stays right.

A model with side effects only its own application knows about overrides
:meth:`_deduplicate_merge_records`; bridge modules do so for contacts and CRM
leads.
"""

import logging
from collections import namedtuple

import psycopg2
from markupsafe import Markup, escape

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

# Models whose merge has side effects that only a dedicated strategy handles.
# A generic merge of one of these is refused unless its bridge is installed,
# or the caller explicitly allows it through ALLOW_GENERIC_CONTEXT.
DEDICATED_MERGE_MODULES = {
    "res.partner": "partner_deduplicate_review",
    "crm.lead": "crm_lead_deduplicate_review",
}
ALLOW_GENERIC_CONTEXT = "deduplicate_allow_generic_merge"
# The engine's own bookkeeping: never re-pointed by the generic pass. The
# group being merged must keep its members as history.
OWN_MODELS = ("deduplicate.record",)

UniqueKey = namedtuple("UniqueKey", "columns has_expression predicate")


def _unique_keys(cr, table):
    """Every unique index on ``table``, primary keys and partial ones included."""
    cr.execute(
        SQL(
            """
            SELECT ARRAY(
                       SELECT a.attname
                         FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, n)
                    LEFT JOIN pg_attribute a
                           ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                     ORDER BY k.n
                   ),
                   i.indexprs IS NOT NULL,
                   pg_get_expr(i.indpred, i.indrelid)
              FROM pg_index i
              JOIN pg_class t ON t.oid = i.indrelid
             WHERE t.relname = %s
               AND t.relnamespace = current_schema::regnamespace
               AND i.indisunique
            """,
            table,
        )
    )
    return [
        UniqueKey(tuple(cols), bool(has_expr), pred)
        for cols, has_expr, pred in cr.fetchall()
    ]


def _column_is_nullable(cr, table, column):
    cr.execute(
        SQL(
            """
            SELECT NOT a.attnotnull
              FROM pg_attribute a
              JOIN pg_class t ON t.oid = a.attrelid
             WHERE t.relname = %s AND a.attname = %s
               AND t.relnamespace = current_schema::regnamespace
            """,
            table,
            column,
        )
    )
    row = cr.fetchone()
    return bool(row and row[0])


def _table_has_id(cr, table):
    cr.execute(
        SQL(
            """
            SELECT 1
              FROM pg_attribute a
              JOIN pg_class t ON t.oid = a.attrelid
             WHERE t.relname = %s AND a.attname = 'id' AND NOT a.attisdropped
               AND t.relnamespace = current_schema::regnamespace
            """,
            table,
        )
    )
    return bool(cr.fetchone())


class Base(models.AbstractModel):
    _inherit = "base"

    # Set by a bridge module on the model it provides a dedicated strategy for.
    _deduplicate_has_strategy = False

    # ------------------------------------------------------------------
    # Identifiers kept for merged-away records
    # ------------------------------------------------------------------

    def _deduplicate_alias_fields(self):
        """Names of the fields whose values must outlive a merged-away record.

        These are written to ``deduplicate.alias`` before the source is removed,
        so an import or synchronisation that identifies records by them can
        resolve a merged record to its master instead of recreating it.

        Default: stored, non-computed Char fields that are not copied and carry
        a single-column unique index. Override to add or remove fields.
        """
        names = [
            name
            for name, field in self._fields.items()
            if field.type == "char"
            and field.store
            and not field.copy
            and not field.compute
            and not field.related
            and name not in models.MAGIC_COLUMNS
        ]
        if not names or not self._auto:
            return []
        unique = {
            key.columns[0]
            for key in _unique_keys(self.env.cr, self._table)
            if len(key.columns) == 1 and not key.has_expression and not key.predicate
        }
        return [name for name in names if name in unique]

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @api.model
    def _deduplicate_check_merge(self, master, sources):
        """Refuse merges that cannot be done safely. Raises UserError."""
        if not master or len(master) != 1:
            raise UserError(self.env._("Choose exactly one record to keep."))
        if not sources:
            raise UserError(
                self.env._("There is nothing to merge into %s.", master.display_name)
            )
        if master._name != self._name or sources._name != self._name:
            raise UserError(self.env._("Only records of the same model can be merged."))
        if master in sources:
            raise UserError(self.env._("The record kept cannot also be merged away."))
        if getattr(type(self), "_disable_data_merge", False):
            raise UserError(
                self.env._(
                    "Records of %s cannot be merged.", self._description or self._name
                )
            )
        merge_method = getattr(self, "_merge_method", None)
        if callable(merge_method):
            result = merge_method(master, sources)
            if isinstance(result, dict) and result.get("error"):
                raise UserError(result["error"])
        module = DEDICATED_MERGE_MODULES.get(self._name)
        if (
            module
            and not self._deduplicate_has_strategy
            and not self.env.context.get(ALLOW_GENERIC_CONTEXT)
        ):
            raise UserError(
                self.env._(
                    "Merging %(model)s needs the module %(module)s, which carries "
                    "the merge logic its application depends on.",
                    model=self._description or self._name,
                    module=module,
                )
            )
        if "deduplicate.alias" not in self.env:
            raise UserError(self.env._("The deduplication engine is not installed."))

    @api.model
    def _deduplicate_merge(self, master, sources, action="unlink", group=None):
        """Merge ``sources`` into ``master`` and remove them.

        Records an alias for every source first, so its identifiers resolve to
        the master afterwards. ``action`` is ``unlink`` or ``archive``.
        Returns the ``deduplicate.alias`` records created.
        """
        master = master.sudo().with_context(active_test=False).exists()
        sources = (sources.sudo().with_context(active_test=False) - master).exists()
        self._deduplicate_check_merge(master, sources)
        if action == "archive" and "active" not in self._fields:
            raise UserError(
                self.env._(
                    "%s has no Active field; merged records cannot be archived.",
                    self._description,
                )
            )
        self.env.flush_all()
        aliases = (
            self.env["deduplicate.alias"]
            .sudo()
            ._deduplicate_record(master, sources, group=group)
        )
        summary = [(source.id, source.display_name) for source in sources]
        self.sudo()._deduplicate_merge_records(master, sources)
        remaining = sources.exists()
        if remaining:
            if action == "archive":
                remaining.write({"active": False})
            else:
                remaining.unlink()
        self.sudo()._deduplicate_log_merge(master, summary, aliases)
        return aliases

    # ------------------------------------------------------------------
    # Strategy: the safe generic merge
    # ------------------------------------------------------------------

    def _deduplicate_merge_records(self, master, sources):
        """Move every reference from ``sources`` to ``master``.

        The sources are removed by the caller afterwards (if a strategy
        already removed them, nothing is left to remove). Override in a model
        whose merge has application-specific side effects.
        """
        return self._deduplicate_merge_generic(master, sources)

    def _deduplicate_merge_generic(self, master, sources):
        cr = self.env.cr
        modified = []  # (model, ids, fname)
        report = {"moved": 0, "dropped": 0}
        model_by_table = {
            model._table: model
            for model in self.env.registry.values()
            if model._auto and not model._abstract
        }
        wizard = self.env["base.partner.merge.automatic.wizard"]
        fks = wizard._get_fk_on(self._table)
        references = list(self._deduplicate_reference_fields())
        # Self-references of the master that point at a SOURCE become
        # self-loops once moved; only those are cleared afterwards. A column
        # that already pointed at the master itself (commercial_partner_id) is
        # legitimate and must be left alone.
        self_columns = [column for table, column in fks if table == self._table]
        to_clear = []
        if self_columns:
            cr.execute(
                SQL(
                    "SELECT %s FROM %s WHERE id = %s",
                    SQL(", ").join(SQL.identifier(c) for c in self_columns),
                    SQL.identifier(self._table),
                    master.id,
                )
            )
            before = dict(zip(self_columns, cr.fetchone(), strict=True))
            to_clear = [
                column
                for column, value in before.items()
                if value in sources.ids and _column_is_nullable(cr, self._table, column)
            ]

        for source in sources:
            for table, column in fks:
                Model = model_by_table.get(table)
                Model = self.env[Model._name] if Model is not None else None
                if Model is not None and Model._name in OWN_MODELS:
                    continue
                if (
                    Model is not None
                    and Model._parent_store
                    and column == Model._parent_name
                ):
                    # parent_path is maintained by write(); raw SQL would leave
                    # the moved subtree's paths pointing at the removed source.
                    cr.execute(
                        SQL(
                            "SELECT id FROM %s WHERE %s = %s",
                            SQL.identifier(table),
                            SQL.identifier(column),
                            source.id,
                        )
                    )
                    ids = [r[0] for r in cr.fetchall()]
                    if ids:
                        Model.sudo().browse(ids).write({column: master.id})
                        report["moved"] += len(ids)
                    continue
                returning = "id" if _table_has_id(cr, table) else None
                moved, dropped = self._deduplicate_repoint(
                    table, column, source.id, master.id, returning=returning
                )
                report["moved"] += len(moved)
                report["dropped"] += dropped
                if Model is not None:
                    if moved and column in Model._fields:
                        modified.append((Model._name, moved, column))
                else:  # a many2many relation table
                    modified.extend(
                        self._deduplicate_m2m_modified(table, column, master)
                    )

            changed, moved_count, dropped = self._deduplicate_repoint_references(
                references, source.id, master.id
            )
            modified.extend(changed)
            report["moved"] += moved_count
            report["dropped"] += dropped

        for column in to_clear:
            # The master pointed at a source and now points at itself.
            cr.execute(
                SQL(
                    "UPDATE %s SET %s = NULL WHERE id = %s AND %s = %s",
                    SQL.identifier(self._table),
                    SQL.identifier(column),
                    master.id,
                    SQL.identifier(column),
                    master.id,
                )
            )
            modified.append((self._name, master.ids, column))
        self._deduplicate_merge_company_dependent(master, sources)
        self.env.invalidate_all()
        for model_name, ids, fname in modified:
            self.env[model_name].sudo().browse(ids).modified([fname])
        self.env.flush_all()
        self._deduplicate_fill_values(master, sources)
        _logger.info(
            "deduplicate: merged %s %s into %s (%s references moved, %s duplicates "
            "dropped)",
            self._name,
            sources.ids,
            master.id,
            report["moved"],
            report["dropped"],
        )
        return report

    def _deduplicate_reference_fields(self):
        """(model, field) for every stored Many2oneReference and Reference field
        that can point at this model."""
        for model in self.env.registry.values():
            if model._abstract or not model._auto or model._name in OWN_MODELS:
                continue
            for field in model._fields.values():
                if not field.store or field.compute:
                    continue
                if (
                    field.type == "many2one_reference"
                    and field.model_field
                    or field.type == "reference"
                ):
                    yield self.env[model._name], field

    def _deduplicate_m2o_scope(self, field):
        """Scope for a Many2oneReference column: rows that name this model."""
        model_column = SQL.identifier(field.model_field)

        def scope(alias):
            return SQL("%s.%s = %s", alias, model_column, self._name)

        return scope

    def _deduplicate_repoint_references(self, references, source_id, master_id):
        """Re-point every Many2oneReference and Reference field from the source.

        Returns (to mark modified, rows moved, duplicates dropped).
        """
        cr = self.env.cr
        modified, moved_count, dropped = [], 0, 0
        for Model, field in references:
            if field.type == "many2one_reference":
                moved, gone = self._deduplicate_repoint(
                    Model._table,
                    field.name,
                    source_id,
                    master_id,
                    scope=self._deduplicate_m2o_scope(field),
                    returning="id",
                )
                dropped += gone
            else:  # reference: 'model,id'
                cr.execute(
                    SQL(
                        "UPDATE %s SET %s = %s WHERE %s = %s RETURNING id",
                        SQL.identifier(Model._table),
                        SQL.identifier(field.name),
                        f"{self._name},{master_id}",
                        SQL.identifier(field.name),
                        f"{self._name},{source_id}",
                    )
                )
                moved = [r[0] for r in cr.fetchall()]
            moved_count += len(moved)
            if moved:
                modified.append((Model._name, moved, field.name))
        return modified, moved_count, dropped

    def _deduplicate_drop_conflicts_everywhere(
        self, fks, references, source_id, master_id
    ):
        """Remove only the source's rows that duplicate the master's, in every
        table referencing this model, without moving anything.

        For a strategy that hands the move itself to another merge (the core
        contact merge): once these rows are gone, that merge's UPDATEs cannot
        collide, so it never falls back to deleting all of a source's rows.
        """
        dropped = 0
        own = {self.env[name]._table for name in OWN_MODELS if name in self.env}
        for table, column in fks:
            if table not in own:
                dropped += self._deduplicate_drop_conflicts(
                    table, column, source_id, master_id
                )
        for Model, field in references:
            if field.type == "many2one_reference":
                dropped += self._deduplicate_drop_conflicts(
                    Model._table,
                    field.name,
                    source_id,
                    master_id,
                    scope=self._deduplicate_m2o_scope(field),
                )
        return dropped

    def _deduplicate_drop_conflicts(
        self, table, column, source_id, master_id, scope=None
    ):
        """Delete ``table`` rows of the source that would duplicate one of the
        master's under a unique key covering ``column``. Returns how many.

        Raises UserError for a one-to-one key both records use: there is no
        safe automatic answer to which of the two rows survives.
        """
        cr = self.env.cr
        t = SQL.identifier(table)
        col = SQL.identifier(column)

        def scoped(alias):
            return scope(alias) if scope else SQL("TRUE")

        dropped = 0
        for key in _unique_keys(cr, table):
            if column not in key.columns:
                continue
            others = [c for c in key.columns if c != column]
            if key.has_expression or None in others:
                continue  # the guarded UPDATE refuses if it collides
            # A catalog predicate may contain `%` (LIKE 'x%'); SQL() needs it doubled.
            pred = (
                SQL(key.predicate.replace("%", "%%")) if key.predicate else SQL("TRUE")
            )
            if not others:
                cr.execute(
                    SQL(
                        "SELECT count(*) FROM %s AS r WHERE r.%s IN %s AND %s AND %s",
                        t,
                        col,
                        (source_id, master_id),
                        scoped(SQL("r")),
                        pred,
                    )
                )
                if cr.fetchone()[0] > 1:
                    raise UserError(
                        self.env._(
                            "Both records own a row in %(table)s, which allows only "
                            "one per record (%(column)s); resolve it before merging.",
                            table=table,
                            column=column,
                        )
                    )
                continue
            same = SQL(" AND ").join(
                SQL("d.%s = r.%s", SQL.identifier(c), SQL.identifier(c)) for c in others
            )
            # `pred` is unqualified on purpose: in the outer WHERE it binds to
            # `r`, inside the EXISTS to `d`, the innermost relation.
            cr.execute(
                SQL(
                    """
                    DELETE FROM %(t)s AS r
                     WHERE r.%(col)s = %(src)s AND %(scope_r)s AND %(pred)s
                       AND EXISTS (SELECT 1 FROM %(t)s AS d
                                    WHERE d.%(col)s = %(dst)s AND %(scope_d)s
                                      AND %(same)s AND %(pred)s)
                    """,
                    t=t,
                    col=col,
                    src=source_id,
                    dst=master_id,
                    scope_r=scoped(SQL("r")),
                    scope_d=scoped(SQL("d")),
                    same=same,
                    pred=pred,
                )
            )
            dropped += cr.rowcount
        return dropped

    def _deduplicate_repoint(
        self, table, column, source_id, master_id, scope=None, returning=None
    ):
        """Move ``table`` rows from ``column = source_id`` to ``master_id``.

        Rows that would duplicate one the master already has are deleted
        first (:meth:`_deduplicate_drop_conflicts`); everything else moves.
        Returns (moved ids or [], number of duplicates dropped).
        """
        cr = self.env.cr
        dropped = self._deduplicate_drop_conflicts(
            table, column, source_id, master_id, scope=scope
        )

        def scoped(alias):
            return scope(alias) if scope else SQL("TRUE")

        returning_sql = (
            SQL("RETURNING r.%s", SQL.identifier(returning)) if returning else SQL()
        )
        try:
            with cr.savepoint(flush=False):
                cr.execute(
                    SQL(
                        "UPDATE %(t)s AS r SET %(col)s = %(dst)s "
                        "WHERE r.%(col)s = %(src)s AND %(scope)s %(ret)s",
                        t=SQL.identifier(table),
                        col=SQL.identifier(column),
                        dst=master_id,
                        src=source_id,
                        scope=scoped(SQL("r")),
                        ret=returning_sql,
                    )
                )
                moved = [r[0] for r in cr.fetchall()] if returning else []
        except psycopg2.IntegrityError as error:
            raise UserError(
                self.env._(
                    "Rows of %(table)s could not be moved to the record kept "
                    "(%(error)s). Nothing was merged.",
                    table=table,
                    error=error.pgerror or str(error),
                )
            ) from error
        return moved, dropped

    def _deduplicate_m2m_modified(self, table, column, master):
        """(model, ids, fname) to mark modified after a many2many table moved."""
        out = []
        for model in self.env.registry.values():
            if model._abstract or not model._auto:
                continue
            for field in model._fields.values():
                if field.type != "many2many" or not field.store:
                    continue
                if field.relation != table:
                    continue
                if field.column1 == column:
                    out.append((model._name, master.ids, field.name))
                elif field.column2 == column:
                    self.env.cr.execute(
                        SQL(
                            "SELECT %s FROM %s WHERE %s = %s",
                            SQL.identifier(field.column1),
                            SQL.identifier(table),
                            SQL.identifier(column),
                            master.id,
                        )
                    )
                    ids = [r[0] for r in self.env.cr.fetchall()]
                    if ids:
                        out.append((model._name, ids, field.name))
        return out

    def _deduplicate_merge_company_dependent(self, master, sources):
        """Company-dependent values: references held in jsonb, their fallbacks
        in ir_default, and the merged records' own per-company values."""
        cr = self.env.cr
        source_ids = tuple(sources.ids)
        dependents = self.env.registry.many2one_company_dependents.get(self._name, ())
        for field in dependents:
            cr.execute(
                SQL(
                    """
                    UPDATE %(table)s
                       SET %(field)s = (
                           SELECT jsonb_object_agg(key,
                                  CASE WHEN value::int IN %(src)s THEN %(dst)s
                                       ELSE value::int END)
                             FROM jsonb_each_text(%(field)s))
                     WHERE %(field)s IS NOT NULL
                    """,
                    table=SQL.identifier(self.env[field.model_name]._table),
                    field=SQL.identifier(field.name),
                    src=source_ids,
                    dst=master.id,
                )
            )
        cr.execute(
            SQL(
                """
                UPDATE ir_default
                   SET json_value = CASE WHEN json_value::int IN %(src)s
                                         THEN %(dst)s ELSE json_value END
                  FROM ir_model_fields f
                 WHERE f.id = ir_default.field_id
                   AND f.company_dependent AND f.relation = %(model)s
                   AND f.ttype = 'many2one' AND json_value ~ '^[0-9]+$'
                """,
                src=source_ids,
                dst=str(master.id),
                model=self._name,
            )
        )
        for fname, field in self._fields.items():
            if not field.company_dependent or not field.store:
                continue
            cr.execute(
                SQL(
                    """
                    WITH source AS (
                        SELECT %(field)s FROM %(table)s
                         WHERE id IN %(src)s ORDER BY id
                    ), source_agg AS (
                        SELECT jsonb_object_agg(key, value) AS value
                          FROM source, jsonb_each(%(field)s)
                    )
                    UPDATE %(table)s
                       SET %(field)s = source_agg.value
                                       || COALESCE(%(table)s.%(field)s, '{}'::jsonb)
                      FROM source_agg
                     WHERE id = %(dst)s AND source_agg.value IS NOT NULL
                    """,
                    table=SQL.identifier(self._table),
                    field=SQL.identifier(fname),
                    src=source_ids,
                    dst=master.id,
                )
            )

    def _deduplicate_fill_values(self, master, sources):
        """Give the master the sources' values for fields it leaves empty.

        Booleans and numbers are left alone (False and 0 are values), as are
        fields that are computed, related, not copied, or relational to many.
        """
        skipped = {"boolean", "integer", "float", "monetary", "one2many", "many2many"}
        skipped |= {"binary", "json", "properties", "properties_definition"}
        vals = {}
        for name, field in master._fields.items():
            if (
                not field.store
                or field.compute
                or field.related
                or not field.copy
                or field.type in skipped
                or name in models.MAGIC_COLUMNS
                or field.company_dependent
                or master[name]
            ):
                continue
            for source in sources:
                value = source[name]
                if not value:
                    continue
                if field.type == "many2one":
                    if field.comodel_name == self._name and value in sources | master:
                        continue
                    value = value.id
                vals[name] = value
                break
        if vals:
            master.write(vals)

    def _deduplicate_log_merge(self, master, summary, aliases):
        if not hasattr(master, "message_post"):
            return
        items = Markup("").join(
            Markup("<li>%s (#%s)</li>") % (escape(name), rid) for rid, name in summary
        )
        idents = aliases.identifier_ids
        extra = (
            Markup("<p>%s %s</p>")
            % (
                self.env._("Their identifiers now resolve here:"),
                ", ".join(f"{i.field_name}={i.value}" for i in idents),
            )
            if idents
            else Markup("")
        )
        master.message_post(
            body=Markup("<p>%s</p><ul>%s</ul>%s")
            % (self.env._("Merged into this record:"), items, extra),
            subtype_xmlid="mail.mt_note",
        )

    # ------------------------------------------------------------------
    # Review a hand-picked selection
    # ------------------------------------------------------------------

    def _deduplicate_review_selection(self):
        """Open the records in ``self`` as one group to review.

        Bound to a model's list as a server action, so users who spot
        duplicates send them to review instead of merging on the spot.
        """
        Group = self.env["deduplicate.group"]
        Group._check_reviewer()
        records = self.exists()
        if len(records) < 2:
            raise UserError(self.env._("Select at least two records to review."))
        group = Group._create_manual_group(records)
        return {
            "type": "ir.actions.act_window",
            "res_model": "deduplicate.group",
            "res_id": group.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Usage ("Used In")
    # ------------------------------------------------------------------

    def _deduplicate_usage(self):
        """{record id: {label: count}} of the rows that reference each record."""
        cr = self.env.cr
        usage = {rid: {} for rid in self.ids}
        if not self.ids:
            return usage
        model_by_table = {
            model._table: model
            for model in self.env.registry.values()
            if model._auto and not model._abstract
        }
        wizard = self.env["base.partner.merge.automatic.wizard"]

        def add(label, rows):
            for rid, count in rows:
                if rid in usage and count:
                    usage[rid][label] = usage[rid].get(label, 0) + count

        for table, column in wizard._get_fk_on(self._table):
            model = model_by_table.get(table)
            if model is not None and model._name in OWN_MODELS:
                continue
            label = self._deduplicate_usage_label(model, table, column)
            cr.execute(
                SQL(
                    "SELECT %s, count(*) FROM %s WHERE %s IN %s GROUP BY 1",
                    SQL.identifier(column),
                    SQL.identifier(table),
                    SQL.identifier(column),
                    tuple(self.ids),
                )
            )
            add(label, cr.fetchall())
        for Model, field in self._deduplicate_reference_fields():
            if field.type != "many2one_reference":
                continue
            cr.execute(
                SQL(
                    "SELECT %s, count(*) FROM %s WHERE %s = %s AND %s IN %s GROUP BY 1",
                    SQL.identifier(field.name),
                    SQL.identifier(Model._table),
                    SQL.identifier(field.model_field),
                    self._name,
                    SQL.identifier(field.name),
                    tuple(self.ids),
                )
            )
            add(
                self._deduplicate_usage_label(Model, Model._table, field.name),
                cr.fetchall(),
            )
        return usage

    def _deduplicate_usage_label(self, model, table, column):
        if model is None:
            return f"{table}.{column}"
        field = model._fields.get(column)
        description = self.env[model._name]._description or model._name
        return f"{description} ({field.string if field else column})"
