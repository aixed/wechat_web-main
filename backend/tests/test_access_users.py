import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import access_store
import config
import hook_login
import main
import wechat_api
from ws_manager import ConnectionManager


class AccessUsersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in [patch.object(access_store, 'DB_PATH', self.root / 'access.sqlite3'),
                  patch.object(config, 'config_file_exists', return_value=True),
                  patch.object(hook_login, 'REGISTRY_PATH', self.root / 'instances.json'),
                  patch.object(main, '_SQLITE_CACHE_DIR', str(self.root)),
                  patch.object(main, '_user_caches', {}), patch.object(main, '_account_runtimes', {}),
                  patch.object(config, 'IS_HOOK', True), patch.object(config, 'IS_LOCAL_HOOK', True),
                  patch.object(config, 'IS_PROTOCOL', False), patch.object(config, 'AGENT_WS_ENABLED', False),
                  patch.object(config, 'LOGIN_MODE', 'local_hook'), patch.object(config, 'HOOK_PORT', 30001),
                  patch.object(config, 'HOOK_HOST', '127.0.0.1'), patch.object(config, 'WEB_ACCESS_KEY', 'legacy-test-key')]:
            p.start(); self.addCleanup(p.stop)
        user_token = access_store.current_user.set('')
        account_token = access_store.current_account.set('')
        self.addCleanup(access_store.current_user.reset, user_token)
        self.addCleanup(access_store.current_account.reset, account_token)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.users = {}
        for key in ['alice-test-key', 'bob-test-key']:
            response = await self.client.post('/api/auth/login', json={'key': key})
            self.users[key] = response.json()['user_id']

    def headers(self, key='alice-test-key', account=''):
        return {'X-Access-Key': key, **({'X-Agent-Id': account} if account else {})}

    async def test_arbitrary_keys_create_stable_distinct_sqlite_users_without_plaintext_keys(self):
        again = await self.client.post('/api/auth/login', json={'key': 'alice-test-key'})
        self.assertEqual(self.users['alice-test-key'], again.json()['user_id'])
        self.assertNotEqual(*self.users.values())
        with access_store.connect() as db:
            rows = db.execute('SELECT * FROM access_users').fetchall()
        self.assertFalse(any('alice-test-key' in str(dict(row)) for row in rows))
        self.assertFalse((await self.client.post('/api/auth/login', json={'key': '  '})).json()['ok'])
        self.assertEqual(401, (await self.client.get('/api/accounts', headers={'X-Access-Key': 'unregistered'})).status_code)

    async def test_hook_settings_persist_owner_key_without_returning_it_in_api(self):
        response = await self.client.get('/api/hook/login-settings', headers=self.headers())
        self.assertEqual(200, response.status_code)
        self.assertNotIn('alice-test-key', response.text)
        with access_store.connect() as db:
            row = db.execute('SELECT access_key FROM hook_profiles WHERE user_id=?',
                             (self.users['alice-test-key'],)).fetchone()
        self.assertEqual('alice-test-key', row['access_key'])
        again = await self.client.post('/api/auth/login', json={'key': 'alice-test-key'})
        self.assertEqual(self.users['alice-test-key'], again.json()['user_id'])

    async def test_client_settings_allow_authenticated_user_without_an_account_and_exclude_private_config(self):
        self.assertEqual(401, (await self.client.get('/api/client-settings')).status_code)
        response = await self.client.get('/api/client-settings', headers=self.headers())
        self.assertEqual(200, response.status_code)
        self.assertEqual({'ok', 'public_assets', 'workflow'}, set(response.json()))
        self.assertEqual({'wecom_icon_url'}, set(response.json()['public_assets']))
        self.assertEqual({'self_mention_names'}, set(response.json()['workflow']))

    async def test_ai_settings_expose_configured_defaults_used_by_frontend(self):
        response = await self.client.get('/api/ai/settings', headers={'X-Access-Key': config.WEB_ACCESS_KEY})
        self.assertEqual(200, response.status_code)
        self.assertEqual(config.mcp_defaults_payload(), response.json()['mcp_defaults'])

    async def test_new_key_cannot_see_default_or_use_foreign_ids_in_headers_body_or_routes(self):
        with patch.object(main, '_probe_local_hook_account', AsyncMock()) as probe:
            response = await self.client.get('/api/accounts', headers=self.headers())
        self.assertEqual([], response.json()['accounts'])
        probe.assert_not_awaited()
        for method, path, body, account in [
            ('GET', '/api/self', None, 'default'),
            ('POST', '/api/accounts/activate', {'agent_id': 'default'}, ''),
            ('GET', '/api/hook/instances/default/status', None, ''),
            ('POST', '/api/hook/instances/default/click-login', {}, ''),
            ('POST', '/api/hook/start-wechat', {'instance_id': 'default'}, ''),
            ('POST', '/api/accounts/broadcast/targets', {'agent_ids': ['default']}, ''),
        ]:
            response = await self.client.request(method, path, json=body, headers=self.headers(account=account))
            self.assertEqual(403, response.status_code, (path, response.text))
        with patch.object(wechat_api, 'get_self_info', AsyncMock()) as upstream:
            response = await self.client.get('/api/self', headers=self.headers())
            self.assertEqual(409, response.status_code)
            upstream.assert_not_awaited()

    async def test_parallel_users_keep_runtime_and_persistent_chat_caches_separate(self):
        for key, account in [('alice-test-key', 'alice-account'), ('bob-test-key', 'bob-account')]:
            access_store.claim(account, self.users[key])
        async def read_self():
            current = wechat_api._CURRENT_AGENT_ID.get()
            await asyncio.sleep(.01 if current == 'alice-account' else .001)
            return {'wxid': current}
        with patch.object(wechat_api, 'get_self_info', side_effect=read_self):
            responses = await asyncio.gather(*(self.client.get('/api/self', headers=self.headers(key, account))
                for key, account in [('alice-test-key', 'alice-account'), ('bob-test-key', 'bob-account')]))
        self.assertEqual(['alice-account', 'bob-account'], [r.json()['wxid'] for r in responses])
        stores = []
        for key, account in [('alice-test-key', 'alice-account'), ('bob-test-key', 'bob-account')]:
            access_store.current_user.set(self.users[key])
            with wechat_api.use_agent(account):
                stores.append(main._cache())
        blob = stores[0].put_media_blob(b'alice only', 'image/png', 'a.png')
        self.assertIsNone(stores[1].get_media_blob(blob))
        self.assertNotEqual(stores[0].db_path, stores[1].db_path)

    async def test_rdv_is_persisted_and_distinct_and_in_requested_range(self):
        profiles = []
        for key in self.users:
            first = (await self.client.get('/api/hook/login-settings', headers=self.headers(key))).json()
            again = (await self.client.get('/api/hook/login-settings', headers=self.headers(key))).json()
            self.assertEqual(first['rdv'], again['rdv'])
            self.assertEqual(first['start_port'], again['start_port'])
            self.assertRegex(first['rdv'], '^[0-9a-f]{8}$')
            self.assertTrue(0x01000000 <= int(first['rdv'], 16) <= 0x7fffffff)
            profiles.append(first)
        self.assertNotEqual(profiles[0]['rdv'], profiles[1]['rdv'])
        self.assertNotEqual(profiles[0]['start_port'], profiles[1]['start_port'])
        for value in ['01000000', '7fffffff', '0abcdef0']:
            self.assertEqual(value, access_store.validate_rdv(value))
        for value in ['00ffffff', '80000000', 'abcdefg0']:
            with self.assertRaises(ValueError): access_store.validate_rdv(value)

    async def test_restart_validates_owned_process_then_stops_and_starts_with_proxy_and_new_rdv(self):
        access_store.current_user.set(self.users['alice-test-key'])
        profile = hook_login.user_settings()
        old_rdv = profile['rdv']
        calls = []
        terminated_ports = []
        running = False
        pid = 100
        def handler(request):
            nonlocal running, pid
            body = json.loads(request.content)
            calls.append((request.url.path, body))
            if request.url.path == '/Get_WeChatProcessNumber':
                return httpx.Response(200, json={'List': [{'PID': pid, 'Port': profile['port'], 'Par': f'Wechat.exe StartPort={profile["port"]}&RDV={old_rdv}'}] if running else []})
            if request.url.path == '/Get_PortOccupiedInfo': return httpx.Response(200, json={'Occupied': '1' if running else '0'})
            if request.url.path == '/TerminateThisWeChat':
                terminated_ports.append(request.url.port)
                self.assertEqual({}, body)
                running = False
                return httpx.Response(200, json={'code': 0})
            if request.url.path == '/StartWechat':
                running = True; pid += 1
                return httpx.Response(200, json={'success': '1', '进程ID': str(pid)})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(hook_login, 'client', client):
                first = await hook_login.start_for_user(instance_id=profile['id'])
                again = await hook_login.start_for_user(instance_id=profile['id'])
                self.assertEqual(first['pid'], again['pid'])
                self.assertEqual(1, sum(path == '/StartWechat' for path, _ in calls))
                proxy = {'enabled': True, 'ip': '127.0.0.2', 'port': '1080', 'username': 'alice', 'password': 'proxy-secret'}
                result = await hook_login.start_for_user(instance_id=profile['id'], restart=True, rdv='01000001', proxy=proxy)
        starts = [body for path, body in calls if path == '/StartWechat']
        self.assertEqual('01000001', starts[-1]['RDV'])
        self.assertEqual('127.0.0.2', starts[-1]['Proxy_IP'])
        self.assertEqual('proxy-secret', starts[-1]['Proxy_Pwd'])
        self.assertEqual('01000001', access_store.profile(profile['id'])['rdv'])
        self.assertNotEqual(first['pid'], result['pid'])
        self.assertEqual([profile['port']], terminated_ports)
        paths = [path for path, _ in calls]
        self.assertLess(paths.index('/TerminateThisWeChat'), len(paths) - 1)

    async def test_restart_with_time_wait_residue_still_launches_with_saved_user_settings(self):
        access_store.current_user.set(self.users['alice-test-key'])
        profile = hook_login.user_settings()
        profile.update(pid='100')
        access_store.save_profile(profile)
        running = True
        calls = []
        def handler(request):
            nonlocal running
            calls.append(request)
            if request.url.path == '/Get_WeChatProcessNumber':
                return httpx.Response(200, json={'List': [{'PID': 100, 'Port': profile['port'],
                    'Par': f'StartPort={profile["port"]}&RDV={profile["rdv"]}'}] if running else []})
            if request.url.path == '/Get_PortOccupiedInfo':
                return httpx.Response(200, json={'Occupied': '1'})
            if request.url.path == '/TerminateThisWeChat':
                self.assertEqual(profile['port'], request.url.port)
                running = False
                raise httpx.ReadError('WeChat closed the connection', request=request)
            if request.url.path == '/StartWechat':
                return httpx.Response(200, json={'success': '1', '进程ID': '101'})
            return httpx.Response(404)
        proxy = {'enabled': True, 'ip': '127.0.0.1', 'port': '7897'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(hook_login, 'client', client), \
                 patch.object(hook_login, '_tcp_port_is_listening', AsyncMock(return_value=False)):
                result = await self.client.post('/api/hook/start-wechat', json={
                    'instance_id': profile['id'], 'restart': True, 'rdv': '4718a91f', 'proxy': proxy}, headers=self.headers())
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual('101', result.json()['process_id'])
        saved = access_store.profile(profile['id'])
        self.assertEqual('4718a91f', saved['rdv'])
        self.assertEqual('7897', saved['proxy']['port'])
        starts = [r for r in calls if r.url.path == '/StartWechat']
        self.assertEqual(1, len(starts))
        self.assertEqual('4718a91f', json.loads(starts[0].content)['RDV'])

    async def test_mismatched_process_is_never_terminated_and_foreign_profiles_cannot_be_changed(self):
        access_store.current_user.set(self.users['alice-test-key'])
        profile = hook_login.user_settings()
        with patch.object(hook_login.client, 'post', AsyncMock(return_value=httpx.Response(200,
                request=httpx.Request('POST', 'http://test'), json={'List': [{'Port': profile['port'], 'PID': 99, 'Par': 'RDV=7fffffff'}]}))) as post:
            with self.assertRaisesRegex(ValueError, 'RDV'):
                await hook_login.start_for_user(instance_id=profile['id'], restart=True)
        self.assertEqual(1, post.await_count)
        access_store.current_user.set(self.users['bob-test-key'])
        with self.assertRaises(main.HTTPException):
            await hook_login.start_for_user(instance_id=profile['id'], restart=True)

    async def test_websocket_events_are_only_delivered_to_same_user_and_account(self):
        access_store.claim('alice-account', self.users['alice-test-key'])
        access_store.claim('bob-account', self.users['bob-test-key'])
        manager = ConnectionManager()
        alice, bob, other_tab = AsyncMock(), AsyncMock(), AsyncMock()
        await manager.connect(alice, user_id=self.users['alice-test-key'], account_id='alice-account')
        await manager.connect(bob, user_id=self.users['bob-test-key'], account_id='bob-account')
        await manager.connect(other_tab, user_id=self.users['alice-test-key'], account_id='alice-other')
        await manager.broadcast({'type': 'wechat_message', 'data': {'account_id': 'alice-account', 'messages': ['private']}})
        alice.send_text.assert_awaited_once()
        bob.send_text.assert_not_awaited(); other_tab.send_text.assert_not_awaited()

    async def test_media_and_global_settings_cannot_be_read_with_another_key(self):
        access_store.claim('alice-account', self.users['alice-test-key'])
        access_store.current_user.set(self.users['alice-test-key'])
        with wechat_api.use_agent('alice-account'):
            private = Path(main._scoped_dir(main._IMG_CACHE_DIR)) / 'private.png'
            self.addCleanup(private.unlink, missing_ok=True)
            private.write_bytes(b'private')
        access_store.current_user.set('')
        access_store.claim('bob-account', self.users['bob-test-key'])
        response = await self.client.get('/api/media/image', params={'path': str(private)}, headers=self.headers('bob-test-key', 'bob-account'))
        self.assertEqual(403, response.status_code)
        response = await self.client.get('/api/ai/settings', headers=self.headers('bob-test-key'))
        self.assertEqual(403, response.status_code)

    async def test_websocket_rejects_another_users_account_before_sending_initial_state(self):
        access_store.claim('alice-account', self.users['alice-test-key'])
        socket = AsyncMock()
        socket.query_params = {'key': 'bob-test-key', 'agent_id': 'alice-account'}
        await main.websocket_endpoint(socket)
        socket.close.assert_awaited_once()
        socket.accept.assert_not_awaited()
        socket.send_text.assert_not_awaited()

    async def test_protocol_session_bodies_and_upstream_lists_are_scoped_to_the_key(self):
        with patch.object(config, 'IS_PROTOCOL', True), patch.object(config, 'IS_HOOK', False):
            access_store.claim('alice-session', self.users['alice-test-key'])
            access_store.claim('bob-session', self.users['bob-test-key'])
            with access_store.connect() as db:
                db.execute('INSERT INTO access_metadata VALUES(?,?)', ('protocol_migrated:' + access_store.scope(), '1'))
            for path in ['get-qrcode', 'get-login-status', 'get-profile', 'terminate', 'push-login-url']:
                response = await self.client.post('/api/protocol/' + path,
                    json={'session_id': 'alice-session', 'wxid': 'alice'}, headers=self.headers('bob-test-key'))
                self.assertEqual(403, response.status_code, path)
            upstream = {'sessions': [{'session_id': 'alice-session', 'state': 'waiting'}, {'session_id': 'bob-session', 'state': 'waiting'}], 'ok': True}
            with patch.object(main.protocol_api, 'get_session_list', AsyncMock(return_value=upstream)):
                response = await self.client.get('/api/accounts', headers=self.headers('bob-test-key'))
            self.assertEqual(['bob-session'], [a['id'] for a in response.json()['accounts']])
            self.assertNotIn('alice-session', response.text)

    async def test_deleting_requires_confirmation_and_never_accepts_foreign_accounts(self):
        access_store.claim('alice-account', self.users['alice-test-key'])
        with patch.object(hook_login, 'stop_for_user', AsyncMock()) as stop:
            response = await self.client.request('DELETE', '/api/accounts/alice-account', json={'confirmed': False}, headers=self.headers())
            self.assertEqual(400, response.status_code)
            response = await self.client.request('DELETE', '/api/accounts/alice-account', json={'confirmed': True}, headers=self.headers('bob-test-key'))
            self.assertEqual(403, response.status_code)
            response = await self.client.post('/api/accounts/alice-account/logout', headers=self.headers('bob-test-key'))
            self.assertEqual(403, response.status_code)
            stop.assert_not_awaited()

    async def test_delete_purges_only_the_selected_wechat_owner_and_preserves_shared_blobs(self):
        cache = main._user_cache(self.users['alice-test-key'])
        private = cache.put_media_blob(b'private', owner_wxid='wxid_a')
        shared = cache.put_media_blob(b'shared', owner_wxid='wxid_a')
        cache.put_media_blob(b'shared', owner_wxid='wxid_other')
        for owner in ['wxid_a', 'wxid_other']:
            cache.upsert_messages('room', [{'id': '1', 'msg': owner, 'msgtype': '3', 'db_image_id': shared}], owner_wxid=owner)
            cache.upsert_contacts({'friend': {'name': owner}}, owner_wxid=owner)
            cache.upsert_smart_reply_config({'chat_id': 'room'}, owner_wxid=owner)
        from domp_offline_bridge import OfflineConfirmationStore
        pending = OfflineConfirmationStore(cache.db_path)
        pending.save({'owner_wxid': 'wxid_a', 'chat_id': 'room', 'sender': 'a'}, {'pending': True})
        counts = cache.delete_account_data('wxid_a')
        self.assertGreater(counts['messages'], 0)
        self.assertEqual([], cache.get_messages('room', owner_wxid='wxid_a'))
        self.assertEqual({}, cache.get_contacts(owner_wxid='wxid_a'))
        self.assertEqual([], cache.list_smart_reply_configs(owner_wxid='wxid_a'))
        self.assertIsNone(pending.get({'owner_wxid': 'wxid_a', 'chat_id': 'room', 'sender': 'a'}))
        self.assertIsNone(cache.get_media_blob(private))
        self.assertIsNotNone(cache.get_media_blob(shared))
        self.assertEqual(1, len(cache.get_messages('room', owner_wxid='wxid_other')))
        self.assertTrue(cache.get_contacts(owner_wxid='wxid_other'))

    async def test_logout_keeps_rdv_and_records_but_delete_forgets_only_owned_profile(self):
        user = self.users['alice-test-key']
        access_store.current_user.set(user)
        profile = hook_login.user_settings()
        profile.update(wxid='wxid_a', wxids=['wxid_a'], aliases=[profile['id']], pid='123')
        access_store.save_profile(profile)
        access_store.claim('wxid:wxid_a')
        hook_login.REGISTRY_PATH.write_text(json.dumps([{**profile, 'mode': config.LOGIN_MODE, 'host': config.HOOK_HOST}]), encoding='utf-8')
        cache = main._user_cache(user)
        cache.upsert_messages('room', [{'id': '1', 'msg': 'keep until delete'}], owner_wxid='wxid_a')
        main._runtime_for(profile['id'])['app_state']['self_info'] = {'wxid': 'wxid_a'}
        access_store.current_user.set('')
        with patch.object(hook_login, '_process_for', AsyncMock(return_value={'PID': 123})), patch.object(hook_login, '_stop_process', AsyncMock()) as stop:
            response = await self.client.post(f'/api/accounts/{profile["id"]}/logout', headers={**self.headers(), 'Content-Type': 'application/json'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(profile['rdv'], access_store.profile(profile['id'], user)['rdv'])
            self.assertTrue(cache.get_messages('room', owner_wxid='wxid_a'))
            self.assertEqual(1, stop.await_count)
        with patch.object(hook_login, '_process_for', AsyncMock(return_value=None)):
            response = await self.client.request('DELETE', f'/api/accounts/{profile["id"]}', json={'confirmed': True}, headers=self.headers())
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(access_store.profile(profile['id'], user))
        self.assertIsNone(access_store.owner(profile['id']))
        self.assertTrue(access_store.retired(profile['id']))
        self.assertEqual([], cache.get_messages('room', owner_wxid='wxid_a'))
        self.assertEqual([], hook_login.instances())

    def pending_hook_profile(self, **overrides):
        user = self.users['alice-test-key']
        token = access_store.current_user.set(user)
        try:
            profile = hook_login.user_settings()
            profile.update(pid='123', **overrides)
            access_store.save_profile(profile)
            hook_login.REGISTRY_PATH.write_text(json.dumps([{**profile, 'mode': config.LOGIN_MODE, 'host': config.HOOK_HOST}]), encoding='utf-8')
            return profile
        finally:
            access_store.current_user.reset(token)

    async def test_logout_removes_never_logged_in_instance_and_revokes_qrcode_access(self):
        profile = self.pending_hook_profile()
        response = httpx.Response(200, json={'onlinestatus': '0'})
        with patch.object(hook_login, 'post', AsyncMock(return_value=response)), \
             patch.object(hook_login, '_process_for', AsyncMock(return_value={'PID': 123})), \
             patch.object(hook_login, '_stop_process', AsyncMock()) as stop:
            result = await self.client.post(f'/api/accounts/{profile["id"]}/logout', headers=self.headers())
        self.assertEqual(200, result.status_code, result.text)
        self.assertTrue(result.json()['deleted'])
        self.assertFalse(result.json()['ever_logged_in'])
        self.assertIn(profile['id'], result.json()['removed_account_ids'])
        self.assertIsNone(access_store.profile(profile['id'], self.users['alice-test-key']))
        self.assertIsNone(access_store.owner(profile['id']))
        self.assertEqual([], hook_login.instances())
        stop.assert_awaited_once()
        qr = await self.client.post(f'/api/hook/instances/{profile["id"]}/qrcode', json={}, headers=self.headers())
        self.assertEqual(403, qr.status_code)

    async def test_logout_preserves_confirmed_login_history_even_without_cached_wxid(self):
        profile = self.pending_hook_profile(ever_logged_in=True)
        with patch.object(hook_login, 'post', AsyncMock()) as native, \
             patch.object(hook_login, '_process_for', AsyncMock(return_value={'PID': 123})), \
             patch.object(hook_login, '_stop_process', AsyncMock()):
            result = await self.client.post(f'/api/accounts/{profile["id"]}/logout', headers=self.headers())
        self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['deleted'])
        self.assertTrue(result.json()['ever_logged_in'])
        saved = access_store.profile(profile['id'], self.users['alice-test-key'])
        self.assertEqual(profile['rdv'], saved['rdv'])
        self.assertEqual('', saved['pid'])
        self.assertTrue(access_store.owns(profile['id'], self.users['alice-test-key']))
        native.assert_not_awaited()

    async def test_login_completed_since_last_poll_is_remembered_before_logout(self):
        profile = self.pending_hook_profile()
        responses = [httpx.Response(200, json={'onlinestatus': '3', 'selfwxid': 'wxid_recent'}),
                     httpx.Response(200, json={'wxid': 'wxid_recent', 'nickname': 'Recent login'})]
        with patch.object(hook_login, 'post', AsyncMock(side_effect=responses)), \
             patch.object(hook_login, '_process_for', AsyncMock(return_value={'PID': 123})), \
             patch.object(hook_login, '_stop_process', AsyncMock()):
            result = await self.client.post(f'/api/accounts/{profile["id"]}/logout', headers=self.headers())
        self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()['deleted'])
        self.assertTrue(result.json()['ever_logged_in'])
        saved = access_store.profile(profile['id'], self.users['alice-test-key'])
        self.assertTrue(saved['ever_logged_in'])
        self.assertEqual('wxid_recent', saved['wxid'])

    async def test_failed_process_termination_keeps_pending_instance_and_ownership(self):
        profile = self.pending_hook_profile()
        response = httpx.Response(200, json={'onlinestatus': '0'})
        with patch.object(hook_login, 'post', AsyncMock(return_value=response)), \
             patch.object(hook_login, '_process_for', AsyncMock(return_value={'PID': 123})), \
             patch.object(hook_login, '_stop_process', AsyncMock(side_effect=ValueError('原微信尚未关闭'))):
            result = await self.client.post(f'/api/accounts/{profile["id"]}/logout', headers=self.headers())
        self.assertEqual(400, result.status_code)
        self.assertIsNotNone(access_store.profile(profile['id'], self.users['alice-test-key']))
        self.assertTrue(access_store.owns(profile['id'], self.users['alice-test-key']))
        self.assertEqual(1, len(hook_login.instances()))


if __name__ == '__main__':
    unittest.main()
