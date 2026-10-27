from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


CHECKER = Path(__file__).resolve().parents[2] / 'scripts' / 'check-public-config.py'


class PublicConfigGuardTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.run_command('git', 'init', '-q')
        (self.root/'.gitignore').write_text('config.yaml\nprompts.yaml\nbackend/db/*\n', encoding='utf-8')
        (self.root/'config.example.yaml').write_text(
            'mcp_connections:\n- id: local_mcp\n  name: mcp系统名称\n'
            '  url: http://127.0.0.1:5222/mcp\n  token: ""\n'
            'mcp_defaults:\n  name: mcp系统名称\n', encoding='utf-8')
        (self.root/'config.yaml').write_text(
            'mcp_connections:\n- id: local_mcp\n  name: tenant-private-system\n', encoding='utf-8')

    def run_command(self, *args):
        return subprocess.run(args, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def check(self):
        self.run_command('git', 'add', '.')
        return self.run_command(sys.executable, str(CHECKER), '--staged')

    def test_safe_template_passes_without_staging_private_configuration(self):
        self.assertEqual(0, self.check().returncode)

    def test_private_prompt_file_cannot_be_force_added(self):
        (self.root/'prompts.yaml').write_text('private: value\n', encoding='utf-8')
        self.run_command('git', 'add', '-f', 'prompts.yaml')
        result = self.check()
        self.assertEqual(1, result.returncode)
        self.assertIn(b'local runtime configuration', result.stderr)

    def test_mcp_name_in_indentless_sequence_must_be_anonymized(self):
        path = self.root/'config.example.yaml'
        path.write_text(path.read_text(encoding='utf-8').replace('mcp系统名称', 'different-name', 1), encoding='utf-8')
        self.assertEqual(1, self.check().returncode)

    def test_real_system_name_cannot_appear_in_public_documentation(self):
        (self.root/'README.md').write_text('tenant-private-system', encoding='utf-8')
        result = self.check()
        self.assertEqual(1, result.returncode)
        self.assertNotIn(b'tenant-private-system', result.stderr)

    def test_service_urls_and_prompt_literals_are_rejected_in_source(self):
        (self.root/'app.py').write_text('URL = "https://service.test/api"\nPROMPT = "inline instruction"\n', encoding='utf-8')
        result = self.check()
        self.assertEqual(1, result.returncode)
        self.assertIn(b'service addresses', result.stderr)
        self.assertIn(b'prompt templates', result.stderr)

    def test_public_example_credentials_must_be_empty(self):
        path = self.root/'config.example.yaml'
        path.write_text(path.read_text(encoding='utf-8').replace('token: ""', 'token: private-test-token'), encoding='utf-8')
        result = self.check()
        self.assertEqual(1, result.returncode)
        self.assertNotIn(b'private-test-token', result.stderr)


if __name__ == '__main__':
    unittest.main()
