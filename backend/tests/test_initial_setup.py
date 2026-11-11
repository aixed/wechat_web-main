from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import yaml

import access_store
import config
import main
import protocol_api


class InitialSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for replacement in (
            patch.object(config, '_CONFIG_PATH', str(self.root / 'config.yaml')),
            patch.object(config, 'WEB_ACCESS_KEY', 'admin'),
            patch.object(access_store, 'DB_PATH', self.root / 'access.sqlite3'),
            patch.object(access_store, 'migrate_hook'),
            patch.object(main, '_reload_runtime_config', AsyncMock()),
            patch.object(main, '_start_runtime_after_config', AsyncMock(return_value={'ready': True})),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.payload = {
            'key': 'custom-setup-key', 'web_access_key': 'custom-setup-key', 'wechat_mode': 3,
            'host': 'protocol.example', 'api_port': 30001, 'mgr_port': 29999,
            'api_scheme': 'https', 'protocol_session_list_key': 'separate-protocol-test-key',
            'callback_url': 'https://web.example/api/callback', 'remote_ws': 'wss://web.example/agent',
        }

    async def test_verified_custom_key_can_initialize_remote_protocol_with_independent_credentials(self):
        login = await self.client.post('/api/auth/login', json={'key': self.payload['key']})
        self.assertTrue(login.json()['setup_required'])
        response = await self.client.post('/api/setup/initial-config', json=self.payload)
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()['ok'])
        saved = yaml.safe_load((self.root / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(3, saved['wechat_mode'])
        self.assertEqual('https', saved['remote_protocol_api_scheme'])
        self.assertEqual('separate-protocol-test-key', saved['protocol_session_list_key'])
        self.assertEqual(self.payload['remote_ws'], saved['protocol_remote_ws'])
        self.assertEqual(self.payload['callback_url'], saved['protocol_callback_url'])
        self.assertFalse(saved['agent_ws_enabled'])
        self.assertNotIn('separate-protocol-test-key', response.text)

    async def test_unverified_key_cannot_write_configuration(self):
        response = await self.client.post('/api/setup/initial-config', json=self.payload)
        self.assertEqual(401, response.status_code)
        self.assertFalse((self.root / 'config.yaml').exists())

    async def test_protocol_requires_its_own_key_and_reachable_callback_configuration(self):
        await self.client.post('/api/auth/login', json={'key': self.payload['key']})
        for changed in ({'protocol_session_list_key': ''}, {'callback_url': '', 'remote_ws': ''},
                        {'remote_ws': 'http://web.example/agent'}, {'callback_url': 'https://user:pass@web.example/callback'}):
            with self.subTest(changed=changed):
                response = await self.client.post('/api/setup/initial-config', json={**self.payload, **changed})
                self.assertEqual(400, response.status_code)
                self.assertFalse((self.root / 'config.yaml').exists())

    async def test_existing_configuration_cannot_be_overwritten(self):
        path = self.root / 'config.yaml'
        path.write_text('original: value\n', encoding='utf-8')
        response = await self.client.post('/api/setup/initial-config', json=self.payload)
        self.assertEqual('already_configured', response.json()['error'])
        self.assertEqual('original: value\n', path.read_text(encoding='utf-8'))


class ProtocolCallbackContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_passes_remote_ws_and_recv_type_to_go(self):
        post = AsyncMock(return_value={'ok': True, 'session_id': 'session-test'})
        with patch.object(protocol_api, '_post', post):
            await protocol_api.start_wechat(rdv='127f78ac', callback_url='https://web.example/api/callback',
                                            remote_ws='wss://web.example/agent', recv_type=1)
        body = post.await_args.args[1]
        self.assertEqual('wss://web.example/agent', body['RemoteWS'])
        self.assertEqual(1, body['RecvType'])
        self.assertEqual('https://web.example/api/callback', body['CallBackURL'])

    async def test_health_supports_post_only_protocol_versions(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(405)
        client.post.return_value = httpx.Response(200, json={'ok': True})
        with patch.object(protocol_api, 'client', client):
            self.assertTrue((await protocol_api.health())['ok'])
        client.post.assert_awaited_once_with('/health', json={}, timeout=5.0)

    async def test_wrong_protocol_key_has_actionable_error(self):
        with patch.object(protocol_api, '_post', AsyncMock(return_value={'ok': False, 'error': 'invalid key', 'status_code': 403})):
            result = await protocol_api.get_session_list()
        self.assertFalse(result['ok'])
        self.assertIn('protocol_session_list_key', result['error'])

    def test_request_logs_do_not_print_protocol_management_keys(self):
        self.assertNotIn('private-test-key', protocol_api._preview({'key': 'private-test-key'}))
