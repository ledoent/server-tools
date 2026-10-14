# Copyright 2026 Ledoent
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    sentry_client_replay_optout = fields.Boolean(
        string="Disable Sentry session replay",
        help="Sentry session replay records DOM changes, console activity, "
        "and network requests for any session that hits an error. "
        "Enable this to keep that recording off for your own sessions, "
        "regardless of the database-wide Tier 2 toggle. Server-wide error "
        "capture (Tier 0) is unaffected.",
        # 20.0 deleted SELF_READABLE_FIELDS / SELF_WRITEABLE_FIELDS and gates
        # writes on res.users per field instead: _has_field_access denies write
        # unless the field carries user_writeable (res_users.py:586-597).
        # Without this a normal employee cannot set their own opt-out --
        # fields_get even forces readonly=True in the payload -- which is the
        # whole point of the field.
        user_writeable=True,
    )
