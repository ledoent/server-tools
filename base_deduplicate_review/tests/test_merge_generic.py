# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The safe generic merge, on real models.

res.partner is merged here through the generic strategy (explicitly allowed),
because it is referenced in every way the merge must handle: foreign keys
under unique keys and partial unique indexes, a many2many, a self-reference,
Many2oneReference fields on other models, stored computed fields that depend
on the moved references.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import GENERIC, DeduplicateCase


@tagged("post_install", "-at_install")
class TestMergeGeneric(DeduplicateCase):
    def setUp(self):
        super().setUp()
        self.master = self.Partner.create(
            {"name": "DD Master", "email": "m@example.com"}
        )
        self.source = self.Partner.create(
            {"name": "DD Source", "email": "s@example.com", "phone": "5550123"}
        )

    def merge(self, master=None, sources=None, action="unlink"):
        Partner = self.Partner.with_context(**GENERIC)
        return Partner._deduplicate_merge(
            master or self.master, sources or self.source, action=action
        )

    def test_a_shared_notification_is_kept_once_instead_of_aborting(self):
        """mail_notification has a PARTIAL unique index on (message, partner).
        It is not a pg_constraint row, so a merge that only reads constraints
        runs a bare UPDATE into it and aborts the transaction."""
        doc = self.Partner.create({"name": "DD Doc"})
        message = doc.message_post(body="hello")
        Notification = self.env["mail.notification"]
        for partner in self.master | self.source:
            Notification.create(
                {
                    "mail_message_id": message.id,
                    "res_partner_id": partner.id,
                    "notification_type": "inbox",
                }
            )
        other = self.Partner.create({"name": "DD Other doc"}).message_post(body="x")
        Notification.create(
            {
                "mail_message_id": other.id,
                "res_partner_id": self.source.id,
                "notification_type": "inbox",
            }
        )
        self.merge()
        notifications = Notification.search([("res_partner_id", "=", self.master.id)])
        self.assertEqual(
            sorted(notifications.mapped("mail_message_id").ids),
            sorted([message.id, other.id]),
        )

    def test_followers_keep_every_subscription_once(self):
        shared = self.Partner.create({"name": "DD Shared doc"})
        only_source = self.Partner.create({"name": "DD Source-only doc"})
        shared.message_subscribe(partner_ids=(self.master | self.source).ids)
        only_source.message_subscribe(partner_ids=self.source.ids)
        watcher = self.Partner.create({"name": "DD Watcher"})
        watcher2 = self.Partner.create({"name": "DD Watcher 2"})
        self.master.message_subscribe(partner_ids=watcher.ids)
        self.source.message_subscribe(partner_ids=(watcher | watcher2).ids)
        self.merge()
        self.assertEqual(shared.message_partner_ids & self.master, self.master)
        self.assertIn(self.master, only_source.message_partner_ids)
        self.assertEqual(
            set(self.master.message_partner_ids.ids) & {watcher.id, watcher2.id},
            {watcher.id, watcher2.id},
        )
        Followers = self.env["mail.followers"]
        self.assertEqual(
            Followers.search_count(
                [
                    ("res_model", "=", "res.partner"),
                    ("res_id", "=", shared.id),
                    ("partner_id", "=", self.master.id),
                ]
            ),
            1,
        )

    def test_many2many_is_a_union(self):
        Category = self.env["res.partner.category"]
        both, only = (
            Category.create({"name": "DD both"}),
            Category.create({"name": "DD only"}),
        )
        self.master.category_id = both
        self.source.category_id = both | only
        self.merge()
        self.assertEqual(self.master.category_id, both | only)

    def test_children_move_and_their_computed_fields_follow(self):
        child = self.Partner.create({"name": "DD Child", "parent_id": self.source.id})
        self.assertEqual(child.commercial_partner_id, self.source)
        self.merge()
        self.assertEqual(child.parent_id, self.master)
        self.assertEqual(child.commercial_partner_id, self.master)

    def test_chatter_attachments_and_other_references_move(self):
        self.source.message_post(body="from the source")
        attachment = self.env["ir.attachment"].create(
            {"name": "dd.txt", "res_model": "res.partner", "res_id": self.source.id}
        )
        recycle = self.env["data_recycle.model"].create(
            {"name": "DD recycle", "res_model_id": self.partner_model.id}
        )
        suggestion = self.env["data_recycle.record"].create(
            {"recycle_model_id": recycle.id, "res_id": self.source.id}
        )
        self.merge()
        self.assertIn(
            "from the source", "".join(self.master.message_ids.mapped("body"))
        )
        self.assertEqual(attachment.res_id, self.master.id)
        self.assertEqual(suggestion.res_id, self.master.id)

    def test_empty_fields_are_filled_and_set_ones_kept(self):
        self.merge()
        self.assertEqual(self.master.phone, "5550123")
        self.assertEqual(self.master.email, "m@example.com")

    def test_the_merge_is_logged_on_the_master(self):
        self.merge()
        self.assertIn("DD Source", self.master.message_ids[0].body)

    def test_archive_instead_of_delete(self):
        self.merge(action="archive")
        self.assertTrue(self.source.exists())
        self.assertFalse(self.source.active)

    def test_a_master_that_pointed_at_a_source_does_not_point_at_itself(self):
        self.master.parent_id = self.source
        self.merge()
        self.assertFalse(self.master.parent_id)

    def test_parent_store_hierarchies_stay_consistent(self):
        Category = self.env["res.partner.category"]
        keep = Category.create({"name": "DD keep"})
        gone = Category.create({"name": "DD gone"})
        leaf = Category.create({"name": "DD leaf", "parent_id": gone.id})
        partner = self.Partner.create(
            {"name": "DD tagged", "category_id": [(6, 0, gone.ids)]}
        )
        Category._deduplicate_merge(keep, gone)
        self.assertFalse(gone.exists())
        self.assertEqual(leaf.parent_id, keep)
        self.assertTrue(leaf.parent_path.startswith(keep.parent_path))
        self.assertEqual(partner.category_id, keep)

    def test_refusals(self):
        with self.assertRaisesRegex(UserError, "partner_deduplicate_review"):
            self.Partner._deduplicate_merge(self.master, self.source)
        with self.assertRaises(UserError):
            self.merge(sources=self.master)
        Category = self.env["res.partner.category"]
        one, two = Category.create({"name": "DD a"}), Category.create({"name": "DD b"})
        with (
            patch.object(type(Category), "_disable_data_merge", True, create=True),
            self.assertRaisesRegex(UserError, "cannot be merged"),
        ):
            Category._deduplicate_merge(one, two)
        refuse = {"error": "no merging here"}
        with (
            patch.object(
                type(Category), "_merge_method", lambda self, d, s: refuse, create=True
            ),
            self.assertRaisesRegex(UserError, "no merging here"),
        ):
            Category._deduplicate_merge(one, two)
        self.assertTrue(two.exists())

    def test_usage_counts_what_references_a_record(self):
        self.Partner.create({"name": "DD kid", "parent_id": self.source.id})
        self.source.message_post(body="note")
        usage = self.source._deduplicate_usage()[self.source.id]
        self.assertTrue(
            any(
                count
                for label, count in usage.items()
                if "Related Company" in label or "parent" in label.lower()
            )
        )
        self.assertTrue(any("Message" in label for label in usage))

    def test_default_alias_fields_are_unique_uncopied_chars(self):
        # A property, not a list: other modules add such fields to contacts.
        Partner = self.env["res.partner"]
        names = Partner._deduplicate_alias_fields()
        for name in names:
            field = Partner._fields[name]
            self.assertEqual(field.type, "char", name)
            self.assertFalse(field.copy, name)
        # neither is unique, so neither identifies a record on its own
        self.assertNotIn("ref", names)
        self.assertNotIn("email", names)
