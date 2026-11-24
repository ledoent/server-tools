# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from unittest.mock import patch

from odoo.tests import tagged

from .common import GENERIC, DeduplicateCase


@tagged("post_install", "-at_install")
class TestAlias(DeduplicateCase):
    def setUp(self):
        super().setUp()
        self.Alias = self.env["deduplicate.alias"]
        self.Partner = self.Partner.with_context(**GENERIC)
        patcher = patch.object(
            type(self.env["res.partner"]),
            "_deduplicate_alias_fields",
            lambda self: ["ref"],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_merged_identifier_resolves_to_the_master(self):
        master = self.Partner.create({"name": "DD Keep", "ref": "EXT-1"})
        source = self.Partner.create({"name": "DD Gone", "ref": "EXT-2"})
        aliases = self.Partner._deduplicate_merge(master, source)
        self.assertEqual(aliases.source_res_id, source.id)
        self.assertEqual(aliases.identifiers_text, "ref=EXT-2")
        self.assertEqual(
            self.Alias.resolve_identifiers("res.partner", "ref", ["EXT-2", "EXT-9"]),
            {"EXT-2": master.id},
        )
        self.assertEqual(
            self.Alias.resolve_identifiers("res.partner", "ref", prefix="EXT-"),
            {"EXT-2": master.id},
        )

    def test_chains_resolve_to_the_final_record(self):
        a = self.Partner.create({"name": "DD A", "ref": "C-A"})
        b = self.Partner.create({"name": "DD B", "ref": "C-B"})
        c = self.Partner.create({"name": "DD C", "ref": "C-C"})
        self.Partner._deduplicate_merge(b, a)
        self.Partner._deduplicate_merge(c, b)
        self.assertEqual(
            self.Alias.resolve_ids("res.partner", [a.id, b.id]),
            {a.id: c.id, b.id: c.id},
        )
        self.assertEqual(
            self.Alias.resolve_identifiers("res.partner", "ref", ["C-A", "C-B"]),
            {"C-A": c.id, "C-B": c.id},
        )

    def test_prefix_wildcards_are_literal(self):
        master = self.Partner.create({"name": "DD M"})
        source = self.Partner.create({"name": "DD S", "ref": "A_B/1"})
        self.Partner._deduplicate_merge(master, source)
        self.assertEqual(
            self.Alias.resolve_identifiers("res.partner", "ref", prefix="A_B/"),
            {"A_B/1": master.id},
        )
        self.assertEqual(
            self.Alias.resolve_identifiers("res.partner", "ref", prefix="AXB/"), {}
        )
