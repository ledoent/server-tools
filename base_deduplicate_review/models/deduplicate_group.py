# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Duplicate groups and the decisions taken on them.

Two kinds of group:

* **Duplicates** (``cluster``): every member is the same thing. A reviewer
  picks the record to keep; the others are merged into it. Rules produce these.
* **Match to one of** (``candidates``): one source record and the records it
  might be. A reviewer picks which candidate it is, and only the source is
  merged into it; the other candidates are left alone. External matchers
  (``deduplicate_propose``) produce these, e.g. an import matching its records
  against existing ones.

``to_review -> validated -> merged``, or ``discarded``. With
``require_validation`` a reviewer must validate before a manager can merge, so
nothing is merged that one person alone decided. ``merged`` is reachable only
through :meth:`action_merge`.
"""

import json

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError

STATES = [
    ("to_review", "To Review"),
    ("validated", "Validated"),
    ("merged", "Merged"),
    ("discarded", "Discarded"),
]
OPEN_STATES = ("to_review", "validated")
STATE_CONTEXT = "deduplicate_state_change"
SYNC_CONTEXT = "deduplicate_sync"
REVIEWER = "base_deduplicate_review.group_deduplicate_reviewer"
MANAGER = "base_deduplicate_review.group_deduplicate_manager"


def _check_group(env, xmlid):
    if not (env.su or env.user.has_group(xmlid)):
        raise AccessError(env._("You are not allowed to do this."))


class DeduplicateGroup(models.Model):
    _name = "deduplicate.group"
    _description = "Duplicate Group"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(readonly=True)
    rule_id = fields.Many2one(
        "deduplicate.rule", ondelete="cascade", index="btree_not_null", readonly=True
    )
    res_model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade", readonly=True
    )
    res_model_name = fields.Char(
        related="res_model_id.model", string="Model Name", store=True, index=True
    )
    kind = fields.Selection(
        [("cluster", "Duplicates"), ("candidates", "Match to one of")],
        default="cluster",
        required=True,
        readonly=True,
    )
    source = fields.Selection(
        [("rule", "Rule"), ("external", "Proposed"), ("manual", "Selected by hand")],
        default="rule",
        required=True,
        readonly=True,
    )
    external_key = fields.Char(index=True, readonly=True, copy=False)
    match_key = fields.Char(index=True, readonly=True, copy=False)
    record_ids = fields.One2many("deduplicate.record", "group_id", string="Records")
    record_count = fields.Integer(compute="_compute_record_count")
    state = fields.Selection(
        STATES, default="to_review", required=True, index=True, tracking=True
    )
    require_validation = fields.Boolean(default=True, readonly=True)
    score = fields.Float(readonly=True, digits=(16, 2))
    evidence = fields.Json(string="Evidence Data", readonly=True)
    evidence_text = fields.Text(compute="_compute_evidence_text", string="Evidence")
    note = fields.Text()
    validated_by_id = fields.Many2one("res.users", readonly=True)
    validated_on = fields.Datetime(readonly=True)
    merged_by_id = fields.Many2one("res.users", readonly=True)
    merged_on = fields.Datetime(readonly=True)
    discarded_by_id = fields.Many2one("res.users", readonly=True)
    discarded_on = fields.Datetime(readonly=True)
    master_res_id = fields.Integer(compute="_compute_master_res_id", string="Kept ID")
    alias_ids = fields.One2many("deduplicate.alias", "group_id", string="Merged")

    _external_key_uniq = models.Constraint(
        "UNIQUE (res_model_name, external_key)",
        "A group with this key already exists for this model.",
    )

    @api.depends("record_ids")
    def _compute_record_count(self):
        for group in self:
            group.record_count = len(group.record_ids)

    @api.depends("record_ids.is_master", "record_ids.res_id")
    def _compute_master_res_id(self):
        for group in self:
            master = group.record_ids.filtered("is_master")[:1]
            group.master_res_id = master.res_id

    @api.depends("evidence")
    def _compute_evidence_text(self):
        for group in self:
            group.evidence_text = (
                json.dumps(group.evidence, indent=2, sort_keys=True, default=str)
                if group.evidence
                else False
            )

    def write(self, vals):
        if "state" in vals and not self.env.context.get(STATE_CONTEXT):
            raise UserError(self.env._("Use the buttons to change a group's status."))
        return super().write(vals)

    def _set_state(self, state, **extra):
        return self.with_context(**{STATE_CONTEXT: True}).write(
            {"state": state, **extra}
        )

    def _stamp(self, prefix):
        return {f"{prefix}_by_id": self.env.uid, f"{prefix}_on": fields.Datetime.now()}

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def action_validate(self):
        _check_group(self.env, REVIEWER)
        for group in self:
            if group.state != "to_review":
                raise UserError(self.env._("%s is not waiting for review.", group.name))
            group._check_ready()
        self._set_state("validated", **self._stamp("validated"))
        return True

    def action_merge(self):
        _check_group(self.env, MANAGER)
        for group in self:
            if group.state not in OPEN_STATES:
                raise UserError(self.env._("%s cannot be merged any more.", group.name))
            if group.require_validation and group.state != "validated":
                raise UserError(
                    self.env._(
                        "A reviewer must validate %s before it is merged.", group.name
                    )
                )
            group._check_ready()
            master, sources = group._merge_targets()
            action = group.rule_id.merge_action or "unlink"
            self.env[group.res_model_name]._deduplicate_merge(
                master, sources, action=action, group=group
            )
            group._set_state("merged", **group._stamp("merged"))
            group._release(master, sources)
        return True

    def action_discard(self):
        _check_group(self.env, REVIEWER)
        for group in self:
            if group.state not in OPEN_STATES:
                raise UserError(self.env._("%s cannot be discarded.", group.name))
        self._set_state("discarded", **self._stamp("discarded"))
        return True

    def action_reopen(self):
        _check_group(self.env, REVIEWER)
        if any(group.state == "merged" for group in self):
            raise UserError(self.env._("A merged group cannot be reopened."))
        self._set_state(
            "to_review",
            validated_by_id=False,
            validated_on=False,
            discarded_by_id=False,
            discarded_on=False,
        )
        return True

    def action_refresh_usage(self):
        self.record_ids._refresh_usage()
        return True

    def _check_ready(self):
        self.ensure_one()
        members = self.record_ids.filtered(lambda r: not r.excluded)
        masters = members.filtered("is_master")
        if len(masters) != 1:
            raise UserError(self.env._("Choose the record to keep in %s.", self.name))
        if not masters._originals():
            raise UserError(
                self.env._("The record to keep in %s no longer exists.", self.name)
            )
        if self.kind == "candidates":
            sources = self.record_ids.filtered("is_source")
            if len(sources) != 1:
                raise UserError(
                    self.env._("%s needs exactly one source record.", self.name)
                )
            if masters == sources:
                raise UserError(
                    self.env._("Choose which candidate the record in %s is.", self.name)
                )
            if sources.excluded or not sources._originals():
                raise UserError(
                    self.env._("The source record of %s is gone.", self.name)
                )
        elif not (members - masters)._originals():
            raise UserError(self.env._("Nothing is left to merge in %s.", self.name))

    def _merge_targets(self):
        self.ensure_one()
        members = self.record_ids.filtered(lambda r: not r.excluded)
        masters = members.filtered("is_master")
        if self.kind == "candidates":
            sources = self.record_ids.filtered("is_source")
        else:
            sources = members - masters
        return masters._originals(), sources._originals()

    def _release(self, master, sources):
        """Other open groups listing a merged-away record now list the master."""
        rows = (
            self.env["deduplicate.record"]
            .sudo()
            .search(
                [
                    ("res_model_name", "=", self.res_model_name),
                    ("res_id", "in", sources.ids),
                    ("group_id", "!=", self.id),
                    ("group_id.state", "in", OPEN_STATES),
                ]
            )
        )
        sync = rows.with_context(**{SYNC_CONTEXT: True})
        for row in sync:
            twin = row.group_id.record_ids.filtered(
                lambda r, m=master: r.res_id == m.id
            )
            if twin:
                if row.is_master:
                    twin.write({"is_master": True, "excluded": False})
                row.unlink()
            else:
                row.write({"res_id": master.id, "name": master.display_name})
        for group in rows.group_id.exists():
            if len(group.record_ids) < 2 and group.state == "to_review":
                group.unlink()

    # ------------------------------------------------------------------
    # Group creation and synchronisation
    # ------------------------------------------------------------------

    @api.model
    def _check_reviewer(self):
        _check_group(self.env, REVIEWER)

    @api.model
    def _create_manual_group(self, records):
        """A group of hand-picked records; the oldest is kept by default."""
        model = self.env["ir.model"]._get(records._name)
        return (
            self.sudo()
            .with_context(mail_create_nolog=True)
            .create(
                {
                    "name": records.sorted("id")[:1].display_name,
                    "res_model_id": model.id,
                    "source": "manual",
                    "require_validation": True,
                    "record_ids": self._member_vals(records._name, records.ids),
                }
            )
        )

    @api.model
    def _member_vals(self, model_name, ids, master_id=None, source_id=None):
        records = (
            self.env[model_name].sudo().with_context(active_test=False).browse(ids)
        )
        master_id = master_id or (None if source_id else min(ids))
        return [
            Command.create(
                {
                    "res_id": record.id,
                    "name": record.display_name,
                    "is_master": record.id == master_id,
                    "is_source": record.id == source_id,
                }
            )
            for record in records.exists()
        ]

    @api.model
    def _sync_rule_groups(self, rule, clusters):
        """Make the rule's open groups match ``clusters`` {match_key: ids}.

        New clusters become groups. A group still to review follows its
        cluster. A validated group is a person's decision and is left alone. A
        discarded group stays discarded unless new records join it. A group to
        review whose cluster vanished is removed.
        """
        Group = self.sudo().with_context(mail_create_nolog=True)
        existing = Group.search([("rule_id", "=", rule.id), ("state", "!=", "merged")])
        by_key = {group.match_key: group for group in existing}
        model = self.env["ir.model"]._get(rule.res_model_name)
        for key, ids in clusters.items():
            group = by_key.get(key)
            if not group:
                records = self.env[rule.res_model_name].sudo().browse(min(ids))
                Group.create(
                    {
                        "name": records.with_context(active_test=False).display_name,
                        "rule_id": rule.id,
                        "res_model_id": model.id,
                        "match_key": key,
                        "require_validation": rule.require_validation,
                        "score": 100.0,
                        "record_ids": self._member_vals(rule.res_model_name, ids),
                    }
                )
            elif group.state == "to_review":
                group._sync_members(ids)
            elif group.state == "discarded" and not set(ids) <= set(
                group.record_ids.mapped("res_id")
            ):
                group._sync_members(ids)
                group._set_state("to_review", discarded_by_id=False, discarded_on=False)
                group.message_post(
                    body=self.env._("New records match this group; reopened.")
                )
        stale = existing.filtered(
            lambda g: g.state == "to_review" and g.match_key not in clusters
        )
        stale.unlink()

    def _sync_members(self, ids, source_id=None):
        """Add and remove members so the group lists ``ids``; keep the master."""
        self.ensure_one()
        wanted = set(ids)
        rows = self.record_ids.with_context(**{SYNC_CONTEXT: True})
        rows.filtered(lambda r: r.res_id not in wanted).unlink()
        have = set(self.record_ids.mapped("res_id"))
        missing = sorted(wanted - have)
        if missing:
            self.write(
                {
                    "record_ids": self._member_vals(
                        self.res_model_name,
                        missing,
                        master_id=-1,
                        source_id=source_id,
                    )
                }
            )
        if source_id:
            for row in self.record_ids.with_context(**{SYNC_CONTEXT: True}):
                if row.is_source != (row.res_id == source_id):
                    row.write({"is_source": row.res_id == source_id})
        if not self.record_ids.filtered("is_master") and self.kind == "cluster":
            self.record_ids.sorted("res_id")[:1].with_context(
                **{SYNC_CONTEXT: True}
            ).write({"is_master": True})

    # ------------------------------------------------------------------
    # API for external matchers and importers (callable over RPC)
    # ------------------------------------------------------------------

    @api.model
    def deduplicate_propose(self, model_name, proposals):
        """Create or refresh groups proposed by an external matcher.

        ``proposals`` is a list of dicts: ``external_key`` (required, stable
        per proposal), ``kind`` (``cluster`` or ``candidates``), ``res_ids``,
        ``source_res_id`` (candidates), ``master_res_id`` (optional default),
        ``score``, ``evidence``, ``name`` and ``require_validation``.

        A group a person has acted on is never changed. Returns counts.
        """
        _check_group(self.env, MANAGER)
        model = self.env["ir.model"]._get(model_name)
        if not model:
            raise UserError(self.env._("Unknown model %s.", model_name))
        Model = self.env[model_name].sudo().with_context(active_test=False)
        Group = self.sudo().with_context(mail_create_nolog=True)
        counts = {"created": 0, "refreshed": 0, "kept_decided": 0, "skipped": 0}
        keys = [p["external_key"] for p in proposals]
        existing = {
            g.external_key: g
            for g in Group.search(
                [("res_model_name", "=", model_name), ("external_key", "in", keys)]
            )
        }
        for proposal in proposals:
            kind = proposal.get("kind", "cluster")
            source_id = proposal.get("source_res_id") if kind == "candidates" else None
            ids = list(dict.fromkeys(proposal.get("res_ids") or []))
            if source_id and source_id not in ids:
                ids.insert(0, source_id)
            present = Model.browse(ids).exists().ids
            if len(present) < 2 or (source_id and source_id not in present):
                counts["skipped"] += 1
                continue
            vals = {
                "score": proposal.get("score") or 0.0,
                "evidence": proposal.get("evidence") or False,
            }
            if proposal.get("name"):
                vals["name"] = proposal["name"]
            group = existing.get(proposal["external_key"])
            if not group:
                master_id = proposal.get("master_res_id")
                if master_id not in present:
                    master_id = None if source_id else min(present)
                Group.create(
                    {
                        **vals,
                        "name": vals.get("name")
                        or Model.browse(source_id or present[0]).display_name,
                        "res_model_id": model.id,
                        "kind": kind,
                        "source": "external",
                        "external_key": proposal["external_key"],
                        "require_validation": proposal.get("require_validation", True),
                        "record_ids": self._member_vals(
                            model_name,
                            present,
                            master_id=master_id,
                            source_id=source_id,
                        ),
                    }
                )
                counts["created"] += 1
            elif group.state == "to_review":
                group.write(vals)
                group._sync_members(present, source_id=source_id)
                counts["refreshed"] += 1
            else:
                counts["kept_decided"] += 1
        return counts

    @api.model
    def deduplicate_decisions(self, model_name, external_keys=None):
        """{external_key: {state, master_res_id, source_res_ids}} for proposals."""
        domain = [("res_model_name", "=", model_name), ("source", "=", "external")]
        if external_keys is not None:
            domain.append(("external_key", "in", list(external_keys)))
        out = {}
        for group in self.sudo().search(domain):
            members = group.record_ids.filtered(lambda r: not r.excluded)
            master = members.filtered("is_master")[:1]
            if group.kind == "candidates":
                sources = group.record_ids.filtered("is_source")
            else:
                sources = members - master
            out[group.external_key] = {
                "state": group.state,
                "master_res_id": master.res_id or False,
                "source_res_ids": sources.mapped("res_id"),
            }
        return out


class DeduplicateRecord(models.Model):
    _name = "deduplicate.record"
    _description = "Duplicate Group Member"
    _order = "is_source desc, is_master desc, res_id"

    group_id = fields.Many2one(
        "deduplicate.group", required=True, ondelete="cascade", index=True
    )
    res_model_name = fields.Char(
        related="group_id.res_model_name", store=True, index=True
    )
    res_id = fields.Many2oneReference(
        string="Record ID", model_field="res_model_name", required=True, index=True
    )
    name = fields.Char(readonly=True)
    is_master = fields.Boolean(string="Keep", readonly=True)
    is_source = fields.Boolean(string="Source", readonly=True)
    excluded = fields.Boolean(help="Not part of this group: left untouched.")
    group_state = fields.Selection(related="group_id.state")
    group_kind = fields.Selection(related="group_id.kind")
    record_exists = fields.Boolean(compute="_compute_original", string="Exists")
    record_active = fields.Boolean(compute="_compute_original", string="Active")
    values_text = fields.Char(compute="_compute_values_text", string="Matched On")
    usage_count = fields.Integer(string="Used In", readonly=True)
    usage_text = fields.Text(string="Used In (detail)", readonly=True)

    _group_res_uniq = models.Constraint(
        "UNIQUE (group_id, res_id)", "This record is already in the group."
    )

    def write(self, vals):
        if (
            not self.env.context.get(SYNC_CONTEXT)
            and {"excluded", "is_master"} & set(vals)
            and any(row.group_id.state != "to_review" for row in self)
        ):
            raise UserError(self.env._("Reopen the group to change its members."))
        return super().write(vals)

    def _originals(self):
        """The original records still in the database, archived included."""
        if not self:
            return None
        model_name = self[0].res_model_name
        return (
            self.env[model_name]
            .sudo()
            .with_context(active_test=False)
            .browse(self.mapped("res_id"))
            .exists()
        )

    @api.depends("res_id", "res_model_name")
    def _compute_original(self):
        for row in self:
            record = (
                self.env[row.res_model_name]
                .sudo()
                .with_context(active_test=False)
                .browse(row.res_id)
                .exists()
                if row.res_model_name in self.env
                else None
            )
            row.record_exists = bool(record)
            row.record_active = bool(record) and (
                "active" not in record._fields or record.active
            )

    @api.depends("res_id", "group_id.rule_id.line_ids")
    def _compute_values_text(self):
        for row in self:
            rule = row.group_id.rule_id
            record = (
                self.env[row.res_model_name]
                .sudo()
                .with_context(active_test=False)
                .browse(row.res_id)
                .exists()
                if row.res_model_name in self.env
                else None
            )
            if not rule or not record:
                row.values_text = False
                continue
            parts = []
            for line in rule.line_ids.sorted("sequence"):
                value = record[line.field_id.name]
                if hasattr(value, "display_name"):
                    value = value.display_name
                parts.append(f"{line.field_id.field_description}: {value or ''}")
            row.values_text = " · ".join(parts)

    def action_set_master(self):
        self.ensure_one()
        _check_group(self.env, REVIEWER)
        group = self.group_id
        if group.state != "to_review":
            raise UserError(self.env._("Reopen the group to change the record kept."))
        if group.kind == "candidates" and self.is_source:
            raise UserError(
                self.env._("The source cannot be kept as itself; pick a candidate.")
            )
        sync = {SYNC_CONTEXT: True}
        (group.record_ids - self).with_context(**sync).write({"is_master": False})
        self.with_context(**sync).write({"is_master": True, "excluded": False})
        return True

    def action_open_record(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model_name,
            "res_id": self.res_id,
            "view_mode": "form",
            "target": "current",
        }

    def _refresh_usage(self):
        by_model = {}
        for row in self:
            by_model.setdefault(row.res_model_name, []).append(row.id)
        for model_name, row_ids in by_model.items():
            rows = self.browse(row_ids)
            if model_name not in self.env:
                continue
            records = self.env[model_name].sudo().browse(rows.mapped("res_id")).exists()
            usage = records._deduplicate_usage()
            for row in rows.with_context(**{SYNC_CONTEXT: True}):
                detail = usage.get(row.res_id, {})
                row.write(
                    {
                        "usage_count": sum(detail.values()),
                        "usage_text": "\n".join(
                            f"{label}: {count}"
                            for label, count in sorted(detail.items())
                        )
                        or False,
                    }
                )
