from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import runtime_settings


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in (('ROOT', self.root), ('CONFIG_PATH', self.root/'config.yaml'),
                            ('EXAMPLE_PATH', self.root/'config.example.yaml')):
            patcher = patch.object(runtime_settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root/'config.example.yaml').write_text(
            'prompt_config_file: prompts.yaml\nprompt_example_file: prompts.example.yaml\n'
            'mcp_defaults:\n  name: public\n  required_token_ports: [5222]\n', encoding='utf-8')
        (self.root/'prompts.example.yaml').write_text('example: "Default {{value}}"\n', encoding='utf-8')

    def test_private_configuration_overrides_individual_keys_and_keeps_other_defaults(self):
        (self.root/'config.yaml').write_text('mcp_defaults:\n  name: private\n', encoding='utf-8')
        self.assertEqual('private', runtime_settings.setting('mcp_defaults', 'name'))
        self.assertEqual([5222], runtime_settings.setting('mcp_defaults', 'required_token_ports'))
        self.assertEqual('public', runtime_settings.example('mcp_defaults', 'name'))

    def test_missing_local_prompt_file_uses_public_example(self):
        self.assertEqual('Default content', runtime_settings.prompt('example', value='content'))

    def test_private_prompt_preserves_whitespace_and_does_not_expand_inserted_values(self):
        (self.root/'prompts.yaml').write_text('example: "\\nPrivate {{value}}\\n"\n', encoding='utf-8')
        self.assertEqual('\nPrivate {{other}}\n', runtime_settings.prompt('example', value='{{other}}', other='hidden'))

    def test_changed_configuration_is_reloaded(self):
        (self.root/'config.yaml').write_text('custom: before\n', encoding='utf-8')
        self.assertEqual('before', runtime_settings.setting('custom'))
        (self.root/'config.yaml').write_text('custom: after update\n', encoding='utf-8')
        self.assertEqual('after update', runtime_settings.setting('custom'))

    def test_empty_or_missing_prompt_stops_instead_of_reusing_another_instruction(self):
        (self.root/'prompts.yaml').write_text('example: ""\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            runtime_settings.prompt('example')
        with self.assertRaisesRegex(ValueError, 'Missing configured prompt'):
            runtime_settings.prompt('unknown')


if __name__ == '__main__':
    unittest.main()
