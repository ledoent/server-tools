# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import unittest

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import DeduplicateCase

try:
    import phonenumbers
except ImportError:  # pragma: no cover
    phonenumbers = None


@tagged("post_install", "-at_install")
class TestDetection(DeduplicateCase):
    def test_email_mode_normalises_the_address(self):
        a = self.Partner.create(
            {"name": "DD Alice", "email": "Alice <ALICE@Example.com>"}
        )
        b = self.Partner.create({"name": "DD Alice 2", "email": "alice@example.com "})
        c = self.Partner.create({"name": "DD Carol", "email": "carol@example.com"})
        rule = self._rule([("email", "email")])
        rule._find_duplicates()
        group = self._group_of(rule, a)
        self.assertEqual(len(group), 1)
        self.assertEqual(set(group.record_ids.mapped("res_id")), {a.id, b.id})
        self.assertFalse(self._group_of(rule, c))
        self.assertEqual(group.record_ids.filtered("is_master").res_id, a.id)

    def test_ci_mode_ignores_case_accents_and_spacing(self):
        a = self.Partner.create({"name": "DD José  García"})
        b = self.Partner.create({"name": "dd jose garcia"})
        rule = self._rule([("name", "ci")], domain="[('id', 'in', %s)]" % [a.id, b.id])
        rule._find_duplicates()
        self.assertEqual(len(self._group_of(rule, a).record_ids), 2)

    def test_every_line_must_match_and_empty_never_groups(self):
        a = self.Partner.create({"name": "DD Bob", "email": "bob@example.com"})
        self.Partner.create({"name": "DD Bob", "email": "other@example.com"})
        self.Partner.create({"name": "DD Nomail"})
        self.Partner.create({"name": "DD Nomail"})
        rule = self._rule([("name", "exact"), ("email", "email")])
        rule._find_duplicates()
        self.assertFalse(self._group_of(rule, a))
        self.assertFalse(
            self.Group.search([("rule_id", "=", rule.id)]),
            "two records that both lack an e-mail are not duplicates",
        )

    @unittest.skipUnless(phonenumbers, "phonenumbers is not installed")
    def test_phone_mode_compares_e164(self):
        us = self.env.ref("base.us")
        a = self.Partner.create(
            {"name": "DD P1", "phone": "+1 (415) 555-0100", "country_id": us.id}
        )
        b = self.Partner.create(
            {"name": "DD P2", "phone": "415 555 0100", "country_id": us.id}
        )
        rule = self._rule([("phone", "phone")])
        rule._find_duplicates()
        self.assertEqual(
            set(self._group_of(rule, a).record_ids.mapped("res_id")), {a.id, b.id}
        )

    def test_rerun_is_idempotent_and_follows_the_data(self):
        a = self.Partner.create({"name": "DD Dan", "email": "dan@example.com"})
        b = self.Partner.create({"name": "DD Dan 2", "email": "dan@example.com"})
        rule = self._rule([("email", "email")])
        rule._find_duplicates()
        rule._find_duplicates()
        self.assertEqual(self.Group.search_count([("rule_id", "=", rule.id)]), 1)
        c = self.Partner.create({"name": "DD Dan 3", "email": "DAN@example.com"})
        rule._find_duplicates()
        group = self._group_of(rule, a)
        self.assertEqual(set(group.record_ids.mapped("res_id")), {a.id, b.id, c.id})
        # the cluster dissolves: a group still to review goes with it
        (b | c).write({"email": False})
        rule._find_duplicates()
        self.assertFalse(group.exists())

    def test_a_discarded_group_stays_discarded_until_someone_new_matches(self):
        a = self.Partner.create({"name": "DD Eve", "email": "eve@example.com"})
        self.Partner.create({"name": "DD Eve 2", "email": "eve@example.com"})
        rule = self._rule([("email", "email")])
        rule._find_duplicates()
        group = self._group_of(rule, a)
        group.with_user(self.reviewer).action_discard()
        rule._find_duplicates()
        self.assertEqual(group.state, "discarded")
        self.Partner.create({"name": "DD Eve 3", "email": "eve@example.com"})
        rule._find_duplicates()
        self.assertEqual(group.state, "to_review")
        self.assertEqual(len(group.record_ids), 3)

    def test_a_validated_group_is_a_decision_and_is_left_alone(self):
        a = self.Partner.create({"name": "DD Fay", "email": "fay@example.com"})
        self.Partner.create({"name": "DD Fay 2", "email": "fay@example.com"})
        rule = self._rule([("email", "email")])
        rule._find_duplicates()
        group = self._group_of(rule, a)
        group.with_user(self.reviewer).action_validate()
        self.Partner.create({"name": "DD Fay 3", "email": "fay@example.com"})
        rule._find_duplicates()
        self.assertEqual(group.state, "validated")
        self.assertEqual(len(group.record_ids), 2)

    def test_companies_are_kept_apart_unless_cross_company(self):
        other = self.env["res.company"].create({"name": "DD Other Co"})
        a = self.Partner.create({"name": "DD Gil", "email": "gil@example.com"})
        self.Partner.create(
            {"name": "DD Gil 2", "email": "gil@example.com", "company_id": other.id}
        )
        a.company_id = self.env.company
        rule = self._rule([("email", "email")])
        rule._find_duplicates()
        self.assertFalse(self._group_of(rule, a))
        rule.cross_company = True
        rule._find_duplicates()
        self.assertTrue(self._group_of(rule, a))

    def test_a_rule_needs_a_field_and_a_text_mode_needs_text(self):
        with self.assertRaises(ValidationError):
            self.env["deduplicate.rule"].create(
                {"name": "DD empty", "res_model_id": self.partner_model.id}
            )
        with self.assertRaises(ValidationError):
            self._rule([("parent_id", "ci")])
