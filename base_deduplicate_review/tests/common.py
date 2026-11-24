# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user

from odoo.addons.base_deduplicate_review.models.base import ALLOW_GENERIC_CONTEXT, Base

GENERIC = {ALLOW_GENERIC_CONTEXT: True}


class DeduplicateCase(TransactionCase):
    """Fixtures are built here, never taken from demo data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.reviewer = new_test_user(
            cls.env,
            login="dedup_reviewer",
            groups="base.group_user,base_deduplicate_review.group_deduplicate_reviewer",
        )
        cls.manager = new_test_user(
            cls.env,
            login="dedup_manager",
            groups="base.group_user,base_deduplicate_review.group_deduplicate_manager",
        )
        cls.plain_user = new_test_user(cls.env, login="dedup_plain")
        cls.Partner = cls.env["res.partner"]
        cls.Group = cls.env["deduplicate.group"]
        cls.partner_model = cls.env["ir.model"]._get("res.partner")

    def setUp(self):
        super().setUp()
        # res.partner is the vehicle for the GENERIC merge here. A bridge module
        # installed in the same database gives it a dedicated strategy; pin it
        # back to the engine's own, so these tests test the engine wherever
        # they run.
        partner_class = type(self.env["res.partner"])
        for name, value in (
            ("_deduplicate_merge_records", Base._deduplicate_merge_records),
            ("_deduplicate_log_merge", Base._deduplicate_log_merge),
            ("_deduplicate_has_strategy", False),
        ):
            patcher = patch.object(partner_class, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @classmethod
    def _rule(cls, lines, **vals):
        fields = cls.env["ir.model.fields"]
        return cls.env["deduplicate.rule"].create(
            {
                "name": vals.pop("name", "Contacts"),
                "res_model_id": cls.partner_model.id,
                "domain": vals.pop("domain", "[('name', '=like', 'DD %')]"),
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "field_id": fields._get("res.partner", fname).id,
                            "match_mode": mode,
                        },
                    )
                    for fname, mode in lines
                ],
                **vals,
            }
        )

    def _group_of(self, rule, partner):
        return self.Group.search(
            [("rule_id", "=", rule.id), ("record_ids.res_id", "=", partner.id)]
        )

    def _propose(self, key, res_ids, **kw):
        return self.Group.with_user(self.manager).deduplicate_propose(
            "res.partner",
            [{"external_key": key, "res_ids": res_ids, **kw}],
        )
