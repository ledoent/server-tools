# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import ast
import hashlib
import logging
import re
import unicodedata
from collections import defaultdict

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import email_normalize, split_every

_logger = logging.getLogger(__name__)

MATCH_MODES = [
    ("exact", "Exact"),
    ("ci", "Ignoring case, accents and spacing"),
    ("email", "Same e-mail address"),
    ("phone", "Same phone number"),
]
TEXT_TYPES = ("char", "text")
SUPPORTED_TYPES = ("char", "text", "selection", "many2one", "integer", "date")
READ_BATCH = 2000
MIN_PHONE_DIGITS = 7


def _fold(text):
    """Lower case, accents removed, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.casefold()).strip()


class DeduplicateRule(models.Model):
    _name = "deduplicate.rule"
    _description = "Deduplication Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    res_model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False)],
    )
    res_model_name = fields.Char(
        related="res_model_id.model", string="Model Name", store=True, index=True
    )
    domain = fields.Char(string="Filter", default="[]")
    include_archived = fields.Boolean(
        help="Also compare archived records. Off by default: an archived record is "
        "usually one somebody already set aside."
    )
    cross_company = fields.Boolean(
        help="Group records of different companies. Off by default, so each "
        "company's duplicates are reviewed on their own."
    )
    line_ids = fields.One2many(
        "deduplicate.rule.line", "rule_id", string="Match When", copy=True
    )
    require_validation = fields.Boolean(
        default=True,
        help="A reviewer validates each group before a manager can merge it.",
    )
    merge_action = fields.Selection(
        [("unlink", "Delete"), ("archive", "Archive")],
        string="Merged Records",
        default="unlink",
        required=True,
    )
    group_ids = fields.One2many("deduplicate.group", "rule_id")
    group_count = fields.Integer(compute="_compute_group_count", string="To Review")
    last_run = fields.Datetime(readonly=True, copy=False)
    notify_user_ids = fields.Many2many(
        "res.users",
        string="Notify",
        domain=[("share", "=", False)],
        help="Sent a digest when this rule finds new duplicates.",
    )
    notify_frequency = fields.Integer(default=1)
    notify_frequency_period = fields.Selection(
        [("days", "Days"), ("weeks", "Weeks"), ("months", "Months")],
        default="weeks",
    )
    last_notification = fields.Datetime(readonly=True, copy=False)

    _check_notify_frequency = models.Constraint(
        "CHECK(notify_frequency > 0)", "The notification frequency must be positive."
    )

    def _compute_group_count(self):
        counts = dict(
            self.env["deduplicate.group"]._read_group(
                [("rule_id", "in", self.ids), ("state", "=", "to_review")],
                ["rule_id"],
                ["__count"],
            )
        )
        for rule in self:
            rule.group_count = counts.get(rule, 0)

    @api.constrains("res_model_id", "merge_action")
    def _check_model(self):
        for rule in self:
            model = self.env.get(rule.res_model_name)
            if model is None or model._abstract or not model._auto:
                raise ValidationError(
                    self.env._("%s cannot be deduplicated.", rule.res_model_id.name)
                )
            if rule.merge_action == "archive" and "active" not in model._fields:
                raise ValidationError(
                    self.env._(
                        "%s has no Active field: merged records can only be deleted.",
                        rule.res_model_id.name,
                    )
                )

    @api.constrains("active", "line_ids")
    def _check_lines(self):
        for rule in self:
            if rule.active and not rule.line_ids:
                raise ValidationError(
                    self.env._(
                        "Rule %s needs at least one field to match on.", rule.name
                    )
                )

    def write(self, vals):
        if "res_model_id" in vals:
            open_groups = self.env["deduplicate.group"].search(
                [("rule_id", "in", self.ids), ("state", "!=", "merged")]
            )
            open_groups.unlink()
        return super().write(vals)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_find_duplicates(self):
        for rule in self:
            rule._find_duplicates()
        return self.action_open_groups()

    def action_open_groups(self):
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "base_deduplicate_review.action_deduplicate_group"
        )
        if len(self) == 1:
            action["domain"] = [("rule_id", "=", self.id)]
        return action

    @api.model
    def _cron_find_duplicates(self):
        for rule in self.search([("line_ids", "!=", False)]):
            try:
                with self.env.cr.savepoint():
                    rule._find_duplicates()
            except Exception:
                _logger.exception("deduplicate: rule %s failed", rule.id)
        self._notify_new_groups()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _find_duplicates(self):
        """Group the rule's records by its normalised field values.

        Every line must match. A record with an empty value on any line is
        never grouped: two records that both lack an e-mail are not duplicates.
        """
        self.ensure_one()
        if not self.line_ids:
            return {}
        Model = (
            self.env[self.res_model_name]
            .sudo()
            .with_context(active_test=not self.include_archived)
        )
        domain = ast.literal_eval(self.domain or "[]")
        records = Model.search(Domain(domain) if domain else Domain.TRUE, order="id")
        lines = self.line_ids.sorted("sequence")
        fnames = list(dict.fromkeys(lines.mapped("field_id.name")))
        company_field = None
        if (
            not self.cross_company
            and "company_id" in Model._fields
            and Model._fields["company_id"].store
        ):
            company_field = "company_id"
        read_fields = fnames + ([company_field] if company_field else [])

        buckets = defaultdict(list)
        for batch in split_every(READ_BATCH, records.ids):
            chunk = Model.browse(batch)
            rows = {row["id"]: row for row in chunk.read(read_fields, load=None)}
            for record in chunk:
                row = rows[record.id]
                key = []
                for line in lines:
                    value = line._normalize(record, row[line.field_id.name])
                    if value in (None, False, ""):
                        break
                    key.append(value)
                else:
                    if company_field:
                        key.append(("company", row[company_field] or 0))
                    buckets[tuple(key)].append(record.id)

        clusters = {
            self._match_key(key): ids for key, ids in buckets.items() if len(ids) > 1
        }
        self.env["deduplicate.group"]._sync_rule_groups(self, clusters)
        self.last_run = fields.Datetime.now()
        return clusters

    @api.model
    def _match_key(self, key):
        return hashlib.sha1(repr(key).encode(), usedforsecurity=False).hexdigest()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @api.model
    def _notify_new_groups(self):
        periods = {
            "days": lambda n: relativedelta(days=n),
            "weeks": lambda n: relativedelta(weeks=n),
            "months": lambda n: relativedelta(months=n),
        }
        now = fields.Datetime.now()
        for rule in self.search([("notify_user_ids", "!=", False)]):
            delta = periods[rule.notify_frequency_period or "weeks"](
                rule.notify_frequency or 1
            )
            if rule.last_notification and rule.last_notification + delta > now:
                continue
            since = rule.last_notification or (now - delta)
            count = self.env["deduplicate.group"].search_count(
                [
                    ("rule_id", "=", rule.id),
                    ("state", "=", "to_review"),
                    ("create_date", ">=", since),
                ]
            )
            rule.last_notification = now
            if not count:
                continue
            self.env["mail.thread"].message_notify(
                body=self.env["ir.qweb"]._render(
                    "base_deduplicate_review.notification",
                    {
                        "count": count,
                        "model_label": rule.res_model_id.name,
                        "rule": rule,
                    },
                ),
                model=rule._name,
                res_id=rule.id,
                partner_ids=rule.notify_user_ids.partner_id.ids,
                subject=self.env._("Duplicates to review"),
            )


class DeduplicateRuleLine(models.Model):
    _name = "deduplicate.rule.line"
    _description = "Deduplication Rule Field"
    _order = "sequence, id"

    rule_id = fields.Many2one(
        "deduplicate.rule", required=True, ondelete="cascade", index=True
    )
    sequence = fields.Integer(default=10)
    field_id = fields.Many2one(
        "ir.model.fields",
        required=True,
        ondelete="cascade",
        domain="[('model_id', '=', parent.res_model_id), ('store', '=', True), "
        f"('ttype', 'in', {list(SUPPORTED_TYPES)})]",
    )
    match_mode = fields.Selection(
        MATCH_MODES, string="Match", default="exact", required=True
    )

    @api.constrains("field_id", "match_mode", "rule_id")
    def _check_field(self):
        for line in self:
            field = line.field_id
            if field.model_id != line.rule_id.res_model_id:
                raise ValidationError(
                    self.env._(
                        "%s is not a field of the rule's model.",
                        field.field_description,
                    )
                )
            if not field.store or field.ttype not in SUPPORTED_TYPES:
                raise ValidationError(
                    self.env._("%s cannot be matched on.", field.field_description)
                )
            if line.match_mode != "exact" and field.ttype not in TEXT_TYPES:
                raise ValidationError(
                    self.env._(
                        "%(field)s is not text; only an exact match applies.",
                        field=field.field_description,
                    )
                )

    def _normalize(self, record, value):
        """The comparable form of ``value``; falsy means "do not group"."""
        if value in (None, False, ""):
            return False
        mode = self.match_mode
        if mode == "exact":
            return value.strip() if isinstance(value, str) else value
        text = str(value)
        if mode == "ci":
            return _fold(text)
        if mode == "email":
            return email_normalize(text) or False
        if mode == "phone":
            formatted = False
            if hasattr(record, "_phone_format"):
                formatted = record._phone_format(
                    fname=self.field_id.name, force_format="E164"
                )
            if formatted:
                return formatted
            digits = re.sub(r"\D", "", text)
            return digits if len(digits) >= MIN_PHONE_DIGITS else False
        raise UserError(self.env._("Unknown match mode %s.", mode))
