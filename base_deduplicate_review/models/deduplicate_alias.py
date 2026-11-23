# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""What remains of a merged-away record: its identifiers, and where it went.

A merge removes the source record. Anything outside Odoo that identified it
-- an import key, a synchronisation id, a reference in another system -- would
otherwise find nothing and create it again. An alias keeps the source's
identifier values (see ``base._deduplicate_alias_fields``) and resolves them
to the master, following later merges of the master itself.
"""

from odoo import Command, api, fields, models

MAX_HOPS = 20


class DeduplicateAlias(models.Model):
    _name = "deduplicate.alias"
    _description = "Merged Record"
    _order = "id desc"
    _rec_name = "source_name"

    res_model_name = fields.Char(string="Model", required=True, index=True)
    source_res_id = fields.Integer(string="Merged Record ID", required=True, index=True)
    source_name = fields.Char(string="Merged Record")
    master_res_id = fields.Many2oneReference(
        string="Kept Record ID",
        model_field="res_model_name",
        required=True,
        index=True,
    )
    master_name = fields.Char(string="Kept Record", compute="_compute_master_name")
    identifier_ids = fields.One2many(
        "deduplicate.alias.identifier", "alias_id", string="Identifier Values"
    )
    identifiers_text = fields.Char(
        string="Identifiers", compute="_compute_identifiers_text"
    )
    group_id = fields.Many2one(
        "deduplicate.group", ondelete="set null", index="btree_not_null"
    )

    def _compute_master_name(self):
        for alias in self:
            record = (
                self.env[alias.res_model_name]
                .sudo()
                .with_context(active_test=False)
                .browse(alias.master_res_id)
                .exists()
                if alias.res_model_name in self.env
                else None
            )
            alias.master_name = record.display_name if record else False

    @api.depends("identifier_ids.field_name", "identifier_ids.value")
    def _compute_identifiers_text(self):
        for alias in self:
            alias.identifiers_text = ", ".join(
                f"{i.field_name}={i.value}" for i in alias.identifier_ids
            )

    @api.model
    def _deduplicate_record(self, master, sources, group=None):
        """One alias per source, with its identifier values, before removal."""
        fnames = master._deduplicate_alias_fields()
        vals_list = []
        for source in sources:
            identifiers = [
                Command.create({"field_name": fname, "value": str(source[fname])})
                for fname in fnames
                if source[fname]
            ]
            vals_list.append(
                {
                    "res_model_name": master._name,
                    "source_res_id": source.id,
                    "source_name": source.display_name,
                    "master_res_id": master.id,
                    "identifier_ids": identifiers,
                    "group_id": group.id if group else False,
                }
            )
        return self.create(vals_list)

    # ------------------------------------------------------------------
    # Resolution, for code and for external callers (RPC)
    # ------------------------------------------------------------------

    @api.model
    def resolve_ids(self, model_name, res_ids):
        """{record id: id of the record it now is}, following later merges.

        Ids that were never merged away map to themselves. One query per hop
        for the whole batch, so an importer can resolve thousands at once.
        """
        current = {res_id: res_id for res_id in res_ids}
        pending = set(current.values())
        for _hop in range(MAX_HOPS):
            if not pending:
                break
            rows = self.sudo().search_read(
                [
                    ("res_model_name", "=", model_name),
                    ("source_res_id", "in", list(pending)),
                ],
                ["source_res_id", "master_res_id"],
                order="id",
            )
            # Later rows win, if a record id was merged away twice.
            step = {row["source_res_id"]: row["master_res_id"] for row in rows}
            if not step:
                break
            for res_id, now in current.items():
                if now in step and step[now] != now:
                    current[res_id] = step[now]
            pending = {step[i] for i in pending if i in step} - pending
        return current

    @api.model
    def resolve_identifiers(self, model_name, field_name, values=None, prefix=None):
        """{identifier value: id of the record it now is} for merged records.

        Pass ``values`` to resolve those, or ``prefix`` for every merged value
        starting with it (what an importer indexing a key namespace needs).
        """
        domain = [
            ("res_model_name", "=", model_name),
            ("field_name", "=", field_name),
        ]
        if values is not None:
            domain.append(("value", "in", list(values)))
        if prefix:
            escaped = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            domain.append(("value", "=like", escaped + "%"))
        rows = (
            self.env["deduplicate.alias.identifier"]
            .sudo()
            .search_read(domain, ["value", "alias_id"], order="id")
        )
        alias_ids = {row["alias_id"][0] for row in rows}
        masters = {
            alias.id: alias.master_res_id for alias in self.sudo().browse(alias_ids)
        }
        final = self.resolve_ids(model_name, set(masters.values()))
        # Later rows win: a value merged twice resolves to its latest merge.
        return {row["value"]: final[masters[row["alias_id"][0]]] for row in rows}


class DeduplicateAliasIdentifier(models.Model):
    _name = "deduplicate.alias.identifier"
    _description = "Merged Record Identifier"
    _order = "id"

    alias_id = fields.Many2one(
        "deduplicate.alias", required=True, ondelete="cascade", index=True
    )
    res_model_name = fields.Char(
        related="alias_id.res_model_name", store=True, index=True
    )
    field_name = fields.Char(required=True)
    value = fields.Char(required=True, index=True)
