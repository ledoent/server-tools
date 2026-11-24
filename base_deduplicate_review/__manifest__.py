# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Deduplication Review",
    "summary": "Find duplicate records by rule, review them, and merge safely",
    "version": "20.0.1.0.0",
    "author": "Ledo Enterprises, Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "development_status": "Alpha",
    "website": "https://github.com/OCA/server-tools",
    "category": "Productivity/Data Cleaning",
    "license": "AGPL-3",
    "depends": ["mail", "data_recycle", "phone_validation"],
    "data": [
        "security/deduplicate_security.xml",
        "security/ir.access.csv",
        "data/ir_cron_data.xml",
        "views/deduplicate_templates.xml",
        "views/deduplicate_rule_views.xml",
        "views/deduplicate_group_views.xml",
        "views/deduplicate_alias_views.xml",
        "views/deduplicate_menus.xml",
    ],
    "installable": True,
}
