import os
import tempfile
import unittest
from pathlib import Path

import yaml

import config


class AiConfigReloadTests(unittest.TestCase):
    def test_reload_ai_settings_reads_runtime_file_changes(self):
        old_path = config._CONFIG_PATH
        old_values = (
            config.AI_BASE_URL,
            config.AI_API_KEY,
            config.AI_MODEL,
            config.AI_ACTIVE_PROFILE_ID,
            config.AI_PROFILES,
        )
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "config.yaml"
                path.write_text(
                    yaml.safe_dump({
                        "ai_base_url": "https://first.test/v1",
                        "ai_api_key": "first-key",
                        "ai_model": "first-model",
                    }),
                    encoding="utf-8",
                )
                config._CONFIG_PATH = os.fspath(path)
                first = config.reload_ai_settings()
                self.assertEqual("first-model", first["model"])

                path.write_text(
                    yaml.safe_dump({
                        "ai_base_url": "https://second.test",
                        "ai_api_key": "second-key",
                        "ai_model": "second-model",
                    }),
                    encoding="utf-8",
                )
                second = config.reload_ai_settings()
                self.assertEqual("https://second.test", second["base_url"])
                self.assertEqual("second-model", second["model"])
                self.assertTrue(second["api_key_configured"])
        finally:
            config._CONFIG_PATH = old_path
            (
                config.AI_BASE_URL,
                config.AI_API_KEY,
                config.AI_MODEL,
                config.AI_ACTIVE_PROFILE_ID,
                config.AI_PROFILES,
            ) = old_values

    def test_profile_settings_select_active_and_sync_legacy_keys(self):
        old_path = config._CONFIG_PATH
        old_values = (
            config.AI_BASE_URL,
            config.AI_API_KEY,
            config.AI_MODEL,
            config.AI_ACTIVE_PROFILE_ID,
            config.AI_PROFILES,
        )
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "config.yaml"
                path.write_text(
                    yaml.safe_dump({
                        "ai_profiles": [
                            {
                                "id": "first",
                                "name": "First",
                                "base_url": "https://first.test",
                                "api_key": "first-key",
                                "model": "first-model",
                            },
                            {
                                "id": "second",
                                "name": "Second",
                                "base_url": "https://second.test",
                                "api_key": "second-key",
                                "model": "second-model",
                            },
                        ],
                        "ai_active_profile_id": "second",
                    }),
                    encoding="utf-8",
                )
                config._CONFIG_PATH = os.fspath(path)
                loaded = config.reload_ai_settings()
                self.assertEqual("second", loaded["active_profile_id"])
                self.assertEqual("second-model", loaded["model"])
                self.assertEqual(2, len(loaded["profiles"]))

                saved = config.save_ai_settings(
                    profiles=[
                        {
                            "id": "first",
                            "name": "First",
                            "base_url": "https://first.test",
                            "api_key": "first-key",
                            "model": "first-model",
                        },
                        {
                            "id": "second",
                            "name": "Second",
                            "base_url": "https://second.test/v1",
                            "api_key": "second-key",
                            "model": "second-updated",
                        },
                    ],
                    active_profile_id="second",
                )

                self.assertEqual("second", saved["active_profile_id"])
                self.assertEqual("https://second.test/v1", saved["base_url"])
                self.assertEqual("second-updated", saved["model"])
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual("second", document["ai_active_profile_id"])
                self.assertEqual("https://second.test/v1", document["ai_base_url"])
                self.assertEqual("second-key", document["ai_api_key"])
                self.assertEqual("second-updated", document["ai_model"])
                self.assertEqual(2, len(document["ai_profiles"]))
        finally:
            config._CONFIG_PATH = old_path
            (
                config.AI_BASE_URL,
                config.AI_API_KEY,
                config.AI_MODEL,
                config.AI_ACTIVE_PROFILE_ID,
                config.AI_PROFILES,
            ) = old_values


class AiSettingsPayloadTests(unittest.TestCase):
    def test_default_payload_does_not_expose_api_key(self):
        import main

        old_key = config.AI_API_KEY
        try:
            config.AI_API_KEY = "secret-test-key"
            payload = main._ai_settings_payload()
            self.assertTrue(payload["api_key_configured"])
            self.assertNotIn("api_key", payload)
        finally:
            config.AI_API_KEY = old_key

    def test_explicit_payload_includes_api_key(self):
        import main

        old_key = config.AI_API_KEY
        try:
            config.AI_API_KEY = "secret-test-key"
            payload = main._ai_settings_payload(include_api_key=True)
            self.assertEqual("secret-test-key", payload["api_key"])
        finally:
            config.AI_API_KEY = old_key


class CustomSkillNormalizationTests(unittest.TestCase):
    def test_legacy_builtin_task_is_migrated_to_custom_skill(self):
        import main

        task = main.SmartReplyAiTaskRequest(
            id="legacy_task",
            name="Existing task",
            skill_type="sql",
            skill_id="sql_analyzer",
            instruction="Use only this configured instruction",
        )
        normalized = main._normalize_ai_task(task)

        self.assertEqual("custom", normalized["skill_type"])
        self.assertEqual("Existing task", normalized["name"])
        self.assertEqual("Use only this configured instruction", normalized["instruction"])


if __name__ == "__main__":
    unittest.main()
