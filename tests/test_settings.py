from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_info_web.settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_non_secret_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            config_path.write_text(
                """{
                  \"database_path\": \"data/state.sqlite3\",
                  \"enable_product_hunt\": false,
                  \"product_hunt_pages_per_run\": 2,
                  \"enable_summary\": true,
                  \"summary_monthly_budget_cny\": 20,
                  \"github_pages_per_query\": 1,
                  \"github_queries\": [\"topic:llm\"]
                }""",
                encoding="utf-8",
            )
            previous_values = {
                name: os.environ.get(name)
                for name in ("AI_INFO_WEB_DB_PATH", "ENABLE_PRODUCT_HUNT", "ENABLE_SUMMARY")
            }
            os.environ.update(
                {
                    "AI_INFO_WEB_DB_PATH": "override.sqlite3",
                    "ENABLE_PRODUCT_HUNT": "true",
                    "ENABLE_SUMMARY": "false",
                }
            )
            try:
                settings = load_settings(config_path)
            finally:
                for name, value in previous_values.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

        self.assertEqual((config_path.parent / "override.sqlite3").resolve(), settings.database_path)
        self.assertTrue(settings.enable_product_hunt)
        self.assertFalse(settings.enable_summary)
        self.assertEqual(("topic:llm",), settings.github_queries)
