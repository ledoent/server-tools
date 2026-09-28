# flake8: noqa: B902
from typing import ClassVar

from odoo.addons import mrp
from ...odoo_patch import OdooPatch


class PreInitHookPatch(OdooPatch):
    target = mrp
    method_names: ClassVar[list[str]] = ["_pre_init_mrp"]

    def _pre_init_mrp(cr):
        """Don't try to create an existing column on reinstall"""
