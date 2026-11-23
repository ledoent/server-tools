# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import GENERIC, DeduplicateCase


@tagged("post_install", "-at_install")
class TestReview(DeduplicateCase):
    def setUp(self):
        super().setUp()
        self.a = self.Partner.create({"name": "DD Hal", "email": "hal@example.com"})
        self.b = self.Partner.create({"name": "DD Hal 2", "email": "hal@example.com"})
        self.rule = self._rule([("email", "email")])
        self.rule._find_duplicates()
        self.group = self._group_of(self.rule, self.a)

    def test_only_a_reviewer_validates_and_only_a_manager_merges(self):
        with self.assertRaises(AccessError):
            self.group.with_user(self.plain_user).action_validate()
        self.group.with_user(self.reviewer).action_validate()
        self.assertEqual(self.group.state, "validated")
        self.assertEqual(self.group.validated_by_id, self.reviewer)
        with self.assertRaises(AccessError):
            self.group.with_user(self.reviewer).with_context(**GENERIC).action_merge()
        self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertEqual(self.group.state, "merged")
        self.assertFalse(self.b.exists())

    def test_nothing_merges_before_it_is_validated(self):
        with self.assertRaisesRegex(UserError, "validate"):
            self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertTrue(self.b.exists())

    def test_the_status_moves_only_through_the_buttons(self):
        with self.assertRaises(UserError):
            self.group.write({"state": "merged"})

    def test_the_reviewer_chooses_the_record_kept(self):
        row_b = self.group.record_ids.filtered(lambda r: r.res_id == self.b.id)
        row_b.with_user(self.reviewer).action_set_master()
        self.group.with_user(self.reviewer).action_validate()
        self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertFalse(self.a.exists())
        self.assertTrue(self.b.exists())

    def test_an_excluded_member_is_left_alone(self):
        c = self.Partner.create({"name": "DD Hal 3", "email": "hal@example.com"})
        self.rule._find_duplicates()
        row_c = self.group.record_ids.filtered(lambda r: r.res_id == c.id)
        row_c.with_user(self.reviewer).excluded = True
        self.group.with_user(self.reviewer).action_validate()
        self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertTrue(c.exists())
        self.assertFalse(self.b.exists())

    def test_a_decided_group_cannot_be_edited_until_reopened(self):
        self.group.with_user(self.reviewer).action_validate()
        with self.assertRaisesRegex(UserError, "Reopen"):
            self.group.record_ids[0].with_user(self.reviewer).excluded = True
        self.group.with_user(self.reviewer).action_reopen()
        self.assertEqual(self.group.state, "to_review")
        self.assertFalse(self.group.validated_by_id)

    def test_a_merged_group_cannot_be_reopened(self):
        self.group.with_user(self.reviewer).action_validate()
        self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        with self.assertRaises(UserError):
            self.group.with_user(self.reviewer).action_reopen()

    def test_dedicated_models_refuse_a_generic_merge(self):
        self.group.with_user(self.reviewer).action_validate()
        with self.assertRaisesRegex(UserError, "partner_deduplicate_review"):
            self.group.with_user(self.manager).action_merge()

    def test_other_open_groups_follow_the_merge(self):
        c = self.Partner.create({"name": "DD Hal", "phone": "5550100"})
        by_name = self._rule([("name", "exact")], name="By name")
        by_name._find_duplicates()
        other = self._group_of(by_name, c)
        self.assertEqual(set(other.record_ids.mapped("res_id")), {self.a.id, c.id})
        # keep b this time, so a -- a member of the other group -- is merged away
        self.group.record_ids.filtered(lambda r: r.res_id == self.b.id).with_user(
            self.reviewer
        ).action_set_master()
        self.group.with_user(self.reviewer).action_validate()
        self.group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertEqual(set(other.record_ids.mapped("res_id")), {self.b.id, c.id})

    def test_candidates_merge_only_the_source_into_the_chosen_one(self):
        source = self.Partner.create({"name": "DD Imported"})
        one = self.Partner.create({"name": "DD Existing 1"})
        two = self.Partner.create({"name": "DD Existing 2"})
        counts = self._propose(
            "import/1",
            [one.id, two.id],
            kind="candidates",
            source_res_id=source.id,
            evidence={"why": "same name"},
        )
        self.assertEqual(counts["created"], 1)
        group = self.Group.search([("external_key", "=", "import/1")])
        self.assertEqual(group.kind, "candidates")
        self.assertFalse(group.record_ids.filtered("is_master"))
        with self.assertRaisesRegex(UserError, "keep"):
            group.with_user(self.reviewer).action_validate()
        group.record_ids.filtered(lambda r: r.res_id == two.id).with_user(
            self.reviewer
        ).action_set_master()
        group.with_user(self.reviewer).action_validate()
        group.with_user(self.manager).with_context(**GENERIC).action_merge()
        self.assertFalse(source.exists())
        self.assertTrue(one.exists() and two.exists())
        decisions = self.Group.deduplicate_decisions("res.partner", ["import/1"])
        self.assertEqual(decisions["import/1"]["state"], "merged")
        self.assertEqual(decisions["import/1"]["master_res_id"], two.id)

    def test_a_proposal_never_changes_a_decided_group(self):
        x = self.Partner.create({"name": "DD X"})
        y = self.Partner.create({"name": "DD Y"})
        z = self.Partner.create({"name": "DD Z"})
        self._propose("k/1", [x.id, y.id], score=50)
        group = self.Group.search([("external_key", "=", "k/1")])
        self.assertEqual(
            self._propose("k/1", [x.id, y.id, z.id], score=60)["refreshed"], 1
        )
        self.assertEqual(len(group.record_ids), 3)
        self.assertEqual(group.score, 60)
        group.with_user(self.reviewer).action_discard()
        self.assertEqual(
            self._propose("k/1", [x.id, y.id], score=10)["kept_decided"], 1
        )
        self.assertEqual(len(group.record_ids), 3)
        self.assertEqual(self._propose("k/2", [x.id, 999999])["skipped"], 1)

    def test_proposing_needs_a_manager(self):
        with self.assertRaises(AccessError):
            self.Group.with_user(self.reviewer).deduplicate_propose(
                "res.partner",
                [{"external_key": "k/3", "res_ids": [self.a.id, self.b.id]}],
            )

    def test_a_hand_picked_selection_becomes_a_group_to_review(self):
        x = self.Partner.create({"name": "DD Pick 1"})
        y = self.Partner.create({"name": "DD Pick 2"})
        with self.assertRaises(AccessError):
            (x | y).with_user(self.plain_user)._deduplicate_review_selection()
        action = (x | y).with_user(self.reviewer)._deduplicate_review_selection()
        group = self.Group.browse(action["res_id"])
        self.assertEqual(group.source, "manual")
        self.assertEqual(set(group.record_ids.mapped("res_id")), {x.id, y.id})
        self.assertEqual(group.record_ids.filtered("is_master").res_id, x.id)
        with self.assertRaises(UserError):
            x.with_user(self.reviewer)._deduplicate_review_selection()
