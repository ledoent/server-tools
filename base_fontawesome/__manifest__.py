# Copyright 2017 Simone Orsi
# Copyright 2018 Creu Blanca
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

{
    "name": "Base Fontawesome",
    "summary": """Up to date Fontawesome resources.""",
    "version": "20.0.1.0.0",
    "license": "LGPL-3",
    "website": "https://github.com/OCA/server-tools",
    "author": "Camptocamp,Creu Blanca,Odoo Community Association (OCA)",
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "base_fontawesome/static/src/css/fontawesome.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/all.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/v4-shims.css",
        ],
        "web.assets_frontend": [
            "base_fontawesome/static/src/css/fontawesome.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/all.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/v4-shims.css",
        ],
        "web.report_assets_common": [
            "base_fontawesome/static/src/css/fontawesome.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/all.css",
            "base_fontawesome/static/lib/fontawesome-6.7.2/css/v4-shims.css",
        ],
    },
}
