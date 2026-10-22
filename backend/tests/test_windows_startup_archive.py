"""Windows batch files must also work when extracted from GitHub source ZIPs."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BATCH_FILES = ('start-all.bat', 'stop-all.bat', 'scripts/start-frontend-dev.bat')


class WindowsStartupArchiveTests(unittest.TestCase):
    def test_batch_scripts_contain_only_crlf_line_endings_without_bom(self):
        for name in BATCH_FILES:
            with self.subTest(name=name):
                data = (ROOT / name).read_bytes()
                self.assertTrue(data.startswith(b'@echo off\r\n'))
                self.assertNotIn(b'\n', data.replace(b'\r\n', b''), 'bare LF breaks cmd.exe label scanning')
                self.assertNotIn(b'\r', data.replace(b'\r\n', b''))

    def test_git_preserves_batch_blob_bytes_for_source_archives(self):
        attributes = (ROOT / '.gitattributes').read_text(encoding='utf-8')
        rules = {parts[0]: parts[1:] for line in attributes.splitlines() if (parts := line.split()) and not line.startswith('#')}
        self.assertIn('-text', rules.get('*.bat', []))
        self.assertIn('-text', rules.get('*.cmd', []))

    @unittest.skipUnless(os.name == 'nt', 'requires Windows cmd.exe')
    def test_downloaded_startup_can_call_access_key_label_in_path_with_spaces(self):
        # Jump over all setup/launch code: only the safe access-key subroutine runs
        # against a disposable config. No live processes, dependencies or config change.
        with tempfile.TemporaryDirectory(prefix='wechat startup ') as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            shutil.copyfile(ROOT / 'scripts/ensure-web-access-key.ps1', root / 'scripts/ensure-web-access-key.ps1')
            config = root / 'config.yaml'
            config.write_bytes(b'web_access_key: startup-fixture-key\r\n')
            before = config.read_bytes()
            batch = root / 'start-all.bat'
            batch.write_bytes(b'@echo off\r\ngoto deployment_label_probe\r\n' + (ROOT / 'start-all.bat').read_bytes()
                              + b'\r\n:deployment_label_probe\r\nset "ROOT=%~dp0"\r\ncall :ensure_web_access_key\r\nexit /b %ERRORLEVEL%\r\n')
            result = subprocess.run([os.environ['COMSPEC'], '/d', '/c', 'call', str(batch)], capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertEqual(before, config.read_bytes())


if __name__ == '__main__':
    unittest.main()
