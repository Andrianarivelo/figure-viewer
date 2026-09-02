"""Settings defaults and one-time migration tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from figureviewer import app as app_module
from figureviewer import docs as D


class SettingsMigrationTests(unittest.TestCase):
    def _load(self, stored: dict | None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            if stored is not None:
                path.write_text(json.dumps(stored), encoding="utf-8")
            with mock.patch.object(app_module, "SETTINGS_PATH", path):
                return app_module.load_settings()

    def test_new_profile_scans_all_subfolder_levels(self) -> None:
        settings = self._load(None)
        self.assertEqual(settings["scan_depth"], D.DEPTH_UNLIMITED)
        self.assertEqual(settings["video_frame_step"], 10)

    def test_old_top_level_default_migrates_to_all_levels_once(self) -> None:
        settings = self._load({"scan_depth": 0})
        self.assertEqual(settings["scan_depth"], D.DEPTH_UNLIMITED)
        self.assertEqual(settings["settings_schema"], app_module.SETTINGS_SCHEMA_VERSION)

    def test_current_explicit_top_level_choice_is_preserved(self) -> None:
        settings = self._load({
            "settings_schema": app_module.SETTINGS_SCHEMA_VERSION,
            "scan_depth": 0,
        })
        self.assertEqual(settings["scan_depth"], 0)

    def test_schema_two_depth_and_recent_folders_migrate_independently(self) -> None:
        settings = self._load({
            "settings_schema": 2,
            "scan_depth": 0,
            "recent": [r"C:\figures", r"D:\exports"],
        })
        self.assertEqual(settings["scan_depth"], 0)
        self.assertEqual(settings["recent_folders"], [r"C:\figures", r"D:\exports"])
        self.assertEqual(settings["recent_files"], [])
        self.assertNotIn("recent", settings)

    def test_invalid_frame_jump_returns_to_safe_default(self) -> None:
        settings = self._load({
            "settings_schema": "invalid",
            "scan_depth": "invalid",
            "video_frame_step": "invalid",
        })
        self.assertEqual(settings["scan_depth"], D.DEPTH_UNLIMITED)
        self.assertEqual(settings["video_frame_step"], 10)


if __name__ == "__main__":
    unittest.main()
