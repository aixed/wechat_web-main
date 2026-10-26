import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import main
import config
from domp_bridge import WORKFLOW_TOOL, workflow_arguments
from domp_offline_bridge import OfflineConfirmationStore, city_input, confirmation_message, offline_analysis_task, offline_reply_chunks
from mcp_service import McpServiceError
from domp_quote import sql_quote
from domp_unified_bridge import UNIFIED_SKILL_ID
from smart_reply import SmartReplyEngine
from sqlite_cache import SqliteMessageCache

SQL = "UPDATE EINP_BASESTORE.AC99 SET AAC003='索朗多吉' WHERE AAC996 IN (54019910205786) AND aac003='边珍';\nUPDATE EINP_BASICINFO.AC01 SET AAC003='索朗多吉' WHERE AAC001 IN (54019910205786) AND aac003='边珍';"
SOURCE = "--xzsb-2750\n" + SQL
ANALYSIS = {"matched": True, "confidence": 98, "result": json.dumps({"intent": "sql_workflow", "identifier": "2750",
    "risk_level": "medium", "risk_notes": [], "sql_purpose": "更正指定人员姓名"}, ensure_ascii=False)}


class OfflineBridgeTests(unittest.TestCase):
    def test_four_digit_header_is_preserved_but_business_keys_are_not_ids(self):
        args = workflow_arguments(SOURCE, ANALYSIS, {})
        self.assertEqual("2750", args["identifier"])
        self.assertEqual(SOURCE, args["sql_text"])
        for text in (SQL, "2750 2751\n" + SQL, "2750 40386\n" + SQL):
            with self.assertRaises(ValueError):
                workflow_arguments(text, ANALYSIS, {})

    def test_saved_five_digit_prompt_gets_extension_without_losing_custom_rules(self):
        task = {"skill_id": UNIFIED_SKILL_ID, "instruction": "只识别五位旧需求编号。自定义规则。"}
        analyzed = offline_analysis_task(task, SOURCE)
        self.assertIn("自定义规则", analyzed["instruction"])
        self.assertIn("取代上述仅允许五位编号", analyzed["instruction"])
        self.assertEqual("只识别五位旧需求编号。自定义规则。", task["instruction"])

    def test_confirmation_only_accepts_explicit_city_and_never_future_or_negated_actions(self):
        self.assertEqual(("confirm", ""), city_input("是"))
        self.assertEqual(("input", "日喀则市"), city_input("改为日喀则市"))
        self.assertEqual(("input", "拉萨市社保中心"), city_input("拉萨市社保中心"))
        for text in ("不是拉萨市", "明天再按拉萨市", "不要执行拉萨市"):
            self.assertEqual(("", ""), city_input(text))

    def test_generated_prompt_is_never_an_area_input(self):
        pending = {"arguments": {"identifier": "2751"}, "matched_city": "昌都市.边坝县",
            "matched_area_display": "昌都市 / 边坝县", "city_match": {"matched": True}}
        prompt = confirmation_message(pending)
        for text in (prompt, "@远方多远\u2005" + prompt, prompt.replace("@空气", ""),
                     prompt.split("：", 1)[1]):
            self.assertEqual(("", ""), city_input(text, "2751"))

    def test_missing_city_prompt_uses_current_account_mention(self):
        pending = {'arguments': {'identifier': '2774'}, 'owner_mention': 'Aixed'}
        self.assertEqual('线下单 2774 未识别到城市或县区\n请输入城市或县区(示例：@Aixed 拉萨市 或 @Aixed 边坝县)', confirmation_message(pending))
        for owner_only, name in ((False, '测试昵称'), (True, '空气')):
            prompt = confirmation_message({**pending, 'owner_only': owner_only, 'owner_mention': '测试昵称'})
            self.assertIn(f'@{name} 拉萨市 或 @{name} 边坝县', prompt)
            self.assertEqual(('', ''), city_input('@杨宇\u2005' + prompt, '2774'))

    def test_failed_input_lists_supported_cities_and_counties_in_the_relevant_province(self):
        areas = [{'area_name': '四川省.成都市', 'area_code': 'sichuan', 'area_path': ['四川省', '成都市']},
            {'area_name': '西藏自治区.拉萨市', 'area_code': 'lhasa', 'area_path': ['西藏自治区', '拉萨市']},
            {'area_name': '西藏自治区.昌都市.类乌齐县', 'area_code': 'county', 'area_path': ['西藏自治区', '昌都市', '类乌齐县']},
            {'area_name': '未核对县'},
            {'area_name': '西藏自治区', 'area_code': 'province', 'area_path': ['西藏自治区']}]
        pending = {'arguments': {'identifier': '2774'}, 'candidate_text': '西藏未支持县', 'city_input_provided': True,
            'city_match': {'matched': False, 'supported_areas': areas}}
        reply = confirmation_message(pending)
        self.assertIn('系统支持的西藏自治区市/县：', reply)
        self.assertIn('拉萨市、昌都市.类乌齐县', reply)
        self.assertNotIn('四川', reply)
        self.assertNotIn('成都市', reply)
        self.assertNotIn('未核对县', reply)
        self.assertNotIn('是否确认', reply)
        self.assertEqual(('', ''), city_input(reply, '2774'))

    def test_long_supported_list_keeps_ticket_header_in_every_reply_chunk(self):
        areas = [{'area_name': '测试市.' + '测试县' * 5 + str(index), 'area_code': str(index),
            'area_path': ['西藏自治区', '测试市', '测试县' + str(index)]} for index in range(200)]
        reply = confirmation_message({'arguments': {'identifier': '2774'}, 'candidate_text': '错误县',
            'city_input_provided': True, 'city_match': {'matched': False, 'supported_areas': areas}})
        chunks = offline_reply_chunks(reply)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(part.startswith('线下单 2774 ') and len(part) <= 1800 for part in chunks))
        self.assertEqual(reply.partition('\n')[2], ''.join(part.partition('\n')[2] for part in chunks))
        self.assertTrue(all(city_input(part, '2774') == ('', '') for part in chunks))

    def test_quote_reader_preserves_nested_xml_and_does_not_promote_reference_text(self):
        quote = sql_quote({"msgtype": "49", "msg": "<msg><appmsg><type>57</type><title>当前 SQL</title><refermsg><type>3</type><svrid>900</svrid><content><msg><img aeskey='key'/></msg></content></refermsg></appmsg></msg>"})
        self.assertEqual("当前 SQL", quote["text"])
        self.assertEqual("900", quote["image"]["id"])
        self.assertIn("aeskey=", quote["image"]["msg"])


class OfflineConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = SqliteMessageCache(str(Path(self.temp.name) / "cache.sqlite3"))
        self.context = {"owner_wxid": "owner", "chat_id": "room@chatroom", "sender": "sender"}
        self.cache.upsert_group_members(self.context["chat_id"], [{"wxid": "owner", "name": "Aixed"},
            {"wxid": "sender", "name": "杨宇"}], owner_wxid="owner")
        task = {"id": UNIFIED_SKILL_ID + "_text", "skill_id": UNIFIED_SKILL_ID, "name": "数据运维", "enabled": True,
                "message_type": "text", "mcp_enabled": True, "mcp_connection_id": "test", "confidence": 90}
        self.cache.upsert_smart_reply_config({"chat_id": self.context["chat_id"], "enabled": True, "message_types": ["text"],
            "target_senders_by_type": {"text": ["sender", "other"]}, "rules": [], "ai_tasks": [task]}, owner_wxid="owner")
        self.send = AsyncMock(return_value={"SendAtMsg": "1"})
        self.workflow = AsyncMock(return_value={"structured": {"identifier": "2750", "submission": {"status": "填报成功"},
            "approval": {"status": "审核通过"}, "execution": {"status": "执行成功", "complete": True,
            "affected_rows_total": 2, "details": [], "exceptions": []}}})
        async def call(**kwargs):
            if kwargs["tool_name"] == "match_data_maintenance_city":
                cities = ["拉萨市", "日喀则市", "林芝市", "山南市"]
                areas = [{'area_name': city, 'area_code': f'test-city-{index}', 'area_path': ['西藏自治区', city]}
                    for index, city in enumerate(cities)]
                matches = [city for city in cities if city.removesuffix("市") in kwargs["arguments"]["city_text"]]
                return {"structured": {"matched": len(matches) == 1, "city": matches[0] if len(matches) == 1 else "",
                    "area_code": next((a['area_code'] for a in areas if len(matches) == 1 and a['area_name'] == matches[0]), ''),
                    "supported_cities": cities, 'supported_areas': areas}}
            return await self.workflow(**kwargs)
        self.mcp = AsyncMock(side_effect=call)
        self.vision = AsyncMock(return_value={"result": json.dumps({"department": "拉萨市社会保险事业管理局", "city": "拉萨市"})})
        self.quote_image = AsyncMock(return_value="data:image/png;base64," + base64.b64encode(b"original-image").decode())
        for p in [patch.object(main, "sqlite_cache", self.cache), patch.object(main, "smart_reply_engine", SmartReplyEngine(cooldown=0)),
            patch.object(main, "ai_service", type("Ai", (), {"configured": True,
                "analyze": AsyncMock(return_value=ANALYSIS), "analyze_image": self.vision})()),
            patch.object(main.mcp_service, "call_tool", self.mcp), patch.object(main, "_offline_quote_image", self.quote_image),
            patch.object(main, "_smart_reply_image_ocr", AsyncMock(return_value=("", "", "unavailable"))),
            patch.object(main.wechat_api, "send_at", self.send), patch.object(main.manager, "broadcast", AsyncMock()),
            patch.object(main, "_broadcast_local_sent_for_agent", AsyncMock()),
            patch.object(config, "MCP_CONNECTIONS", [{"id": "test", "url": "http://test/mcp", "enabled": True}])]:
            p.start(); self.addCleanup(p.stop)

    async def process(self, text=SOURCE, *, identifier="100", sender="sender", quoted=False, mention=True, mention_name=None, targets=None):
        if mention:
            text = "@" + (mention_name or ("空气" if sender == "owner" else "Aixed")) + "\u2005 " + text
        if quoted:
            import xml.etree.ElementTree as ET
            root = ET.Element("msg"); app = ET.SubElement(root, "appmsg")
            ET.SubElement(app, "type").text = "57"; ET.SubElement(app, "title").text = text
            ref = ET.SubElement(app, "refermsg"); ET.SubElement(ref, "type").text = "3"
            ET.SubElement(ref, "svrid").text = "900"; ET.SubElement(ref, "content").text = "<msg><img/></msg>"
            text = ET.tostring(root, encoding="unicode")
        message = {"id": identifier, "msgtype": "49" if quoted else "1", "msg": text, "fromid": sender, "isSender": 0, "sendorrecv": "2"}
        if targets is not None:
            message["atuserlist"] = targets
        await main._process_smart_reply_message(owner_wxid="owner", self_wxid="owner", agent_id="agent", chat_id="room@chatroom", message=message)

    async def test_quoted_sql_waits_for_city_then_preserves_image_sql_and_original_request(self):
        await self.process(quoted=True)
        self.workflow.assert_not_awaited()
        self.assertIn("匹配区划：拉萨市", self.send.call_args.args[3])
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        original_request = pending["arguments"]["request_id"]
        # Confirmation survives a backend/cache restart.
        with patch.object(main, "sqlite_cache", SqliteMessageCache(self.cache.db_path)):
            await self.process("改为日喀则市", identifier="101")
            self.workflow.assert_awaited_once()
        args = self.mcp.call_args.kwargs["arguments"]
        self.assertEqual("日喀则市", args["city"])
        self.assertEqual(SOURCE, args["sql_text"])
        self.assertEqual(original_request, args["request_id"])
        self.assertEqual(base64.b64encode(b"original-image").decode(), args["attachment_base64"])
        self.assertIn("执行成功", self.send.call_args.args[3])
        await self.process("是", identifier="102")
        self.workflow.assert_awaited_once()

    async def test_send_at_callback_during_send_cannot_overwrite_matched_area(self):
        async def send_at(chat, sender, nickname, reply):
            await main._process_smart_reply_message_unlocked(owner_wxid="owner", self_wxid="owner",
                agent_id="agent", chat_id=chat, message={"id": "send-echo", "msgtype": "1",
                    "msg": "@远方多远\u2005" + reply, "fromid": "owner", "isSender": 1, "sendorrecv": "1"})
            return {"SendAtMsg": "1"}
        self.send.side_effect = send_at
        await self.process(quoted=True)
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        self.assertEqual("拉萨市", pending["matched_city"])
        self.assertEqual("拉萨市社会保险事业管理局", pending["candidate_text"])
        self.assertEqual(1, self.mcp.await_count)
        await self.process("是", identifier="confirmed", sender="owner")
        self.workflow.assert_awaited_once()
        self.assertEqual(3, self.send.await_count)

    async def test_delayed_prompt_after_restart_is_ignored_without_echo_memory(self):
        await self.process(quoted=True)
        prompt = self.send.call_args.args[3]
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        with patch.object(main, "smart_reply_engine", SmartReplyEngine()):
            await self.process("@远方多远\u2005" + prompt, identifier="late-echo", sender="owner", mention=False)
        self.assertEqual(pending, OfflineConfirmationStore(self.cache.db_path).get(self.context))
        self.assertEqual(2, self.send.await_count)

    async def test_explicit_area_input_runs_immediately_even_with_reply_cooldown(self):
        self.quote_image.return_value = ""
        with patch.object(main, "smart_reply_engine", SmartReplyEngine(cooldown=60)):
            await self.process()
            await self.process("是", identifier="first-confirm", sender="owner")
            self.workflow.assert_not_awaited()
            await self.process("林芝市", identifier="area-input", sender="owner")
            self.assertIn("执行成功", self.send.call_args.args[3])
        self.workflow.assert_awaited_once()
        self.assertEqual("林芝市", self.workflow.call_args.kwargs["arguments"]["city"])
        self.assertIn("执行成功", self.send.call_args.args[3])
        self.assertEqual(4, self.send.await_count)

    async def test_duplicate_callback_cannot_reset_area_after_new_input(self):
        await self.process(quoted=True)
        await self.process("林芝市", identifier="area-input")
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        calls, sends = self.mcp.await_count, self.send.await_count
        await self.process(quoted=True)  # Original SQL callback again.
        self.assertEqual(pending, OfflineConfirmationStore(self.cache.db_path).get(self.context))
        self.assertEqual(calls, self.mcp.await_count)
        self.assertEqual(sends, self.send.await_count)
        self.assertEqual(1, main.ai_service.analyze.await_count)

    async def test_old_polluted_pending_is_recovered_before_a_fresh_confirmation(self):
        await self.process(quoted=True)
        store = OfflineConfirmationStore(self.cache.db_path)
        pending = store.get(self.context)
        original_args = pending["arguments"]
        prompt = confirmation_message(pending)
        store.save(self.context, {**pending, "candidate_text": prompt.split("：", 1)[1],
            "matched_city": "", "matched_area_display": "", "city_match": {"matched": False}})
        await self.process("是", identifier="recover", sender="owner")
        self.workflow.assert_not_awaited()
        repaired = store.get(self.context)
        self.assertEqual("拉萨市", repaired["matched_city"])
        self.assertEqual(original_args, repaired["arguments"])
        self.assertIn("是否确认填报", self.send.call_args.args[3])
        await self.process("是", identifier="fresh-confirm", sender="owner")
        self.workflow.assert_awaited_once()

    async def test_missing_image_requests_city_without_vision_and_uses_sql_fallback(self):
        self.quote_image.return_value = ""
        await self.process()
        self.vision.assert_not_awaited(); self.workflow.assert_not_awaited()
        self.mcp.assert_not_awaited()
        self.assertEqual((self.context['chat_id'], 'sender', '杨宇'), self.send.call_args.args[:3])
        self.assertEqual("线下单 2750 未识别到城市或县区\n请输入城市或县区(示例：@Aixed 拉萨市 或 @Aixed 边坝县)", self.send.call_args.args[3])
        await self.process("确认", identifier="101")
        self.workflow.assert_not_awaited()
        await self.process("林芝市", identifier="102")
        self.workflow.assert_awaited_once()
        self.assertEqual("林芝市", self.mcp.call_args.kwargs["arguments"]["city"])
        self.assertNotIn("attachment_base64", self.mcp.call_args.kwargs["arguments"])
        self.assertIn('执行成功', self.send.call_args.args[3])

    async def test_failed_city_input_lists_platform_counties_then_retries_without_confirmation(self):
        self.quote_image.return_value = ''
        await self.process()
        saved_args = OfflineConfirmationStore(self.cache.db_path).get(self.context)['arguments']
        areas = [{'area_name': '西藏自治区.昌都市.类乌齐县', 'area_code': 'verified-county',
            'area_path': ['西藏自治区', '昌都市', '类乌齐县']},
            {'area_name': '西藏自治区.拉萨市', 'area_code': 'verified-city', 'area_path': ['西藏自治区', '拉萨市']}]
        async def call(**kwargs):
            if kwargs['tool_name'] == 'match_data_maintenance_city':
                found = kwargs['arguments']['city_text'] == '类乌齐县'
                return {'structured': {'matched': found, **(areas[0] if found else {}), 'supported_areas': areas}}
            return await self.workflow(**kwargs)
        self.mcp.side_effect = call
        await self.process('错误县', identifier='bad-city')
        self.workflow.assert_not_awaited()
        self.assertIn('系统支持的西藏自治区市/县：', self.send.call_args.args[3])
        self.assertIn('昌都市.类乌齐县', self.send.call_args.args[3])
        with patch.object(main, 'sqlite_cache', SqliteMessageCache(self.cache.db_path)):
            await self.process('类乌齐县', identifier='valid-city')
        self.workflow.assert_awaited_once()
        self.assertEqual({**saved_args, 'city': areas[0]['area_name']}, self.workflow.call_args.kwargs['arguments'])
        self.assertIn('执行成功', self.send.call_args.args[3])
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))
        await self.process('类乌齐县', identifier='valid-city')
        await self.process('类乌齐县', identifier='repeated-city')
        self.workflow.assert_awaited_once()

    async def test_explicit_city_still_requires_original_sender_and_correct_mention(self):
        self.quote_image.return_value = ''
        await self.process()
        for index, kwargs in enumerate(({'mention': False}, {'sender': 'other'}, {'targets': ['someone_else']})):
            await self.process('林芝市', identifier=f'unauthorized-{index}', **kwargs)
        self.workflow.assert_not_awaited()
        await self.process('林芝市', identifier='authorized')
        self.workflow.assert_awaited_once()

    async def test_timeout_after_explicit_city_input_never_replays_on_yes_or_another_city(self):
        self.quote_image.return_value = ''
        await self.process()
        self.workflow.side_effect = McpServiceError('响应超时')
        await self.process('林芝市', identifier='first-city')
        self.assertIn('结果待核对', self.send.call_args.args[3])
        for index, text in enumerate(('是', '林芝市', '日喀则市')):
            await self.process(text, identifier=f'after-timeout-{index}')
        self.workflow.assert_awaited_once()
        self.assertIn('勿重复提交', self.send.call_args.args[3])
        self.assertEqual('林芝市', OfflineConfirmationStore(self.cache.db_path).get(self.context)['confirmed_city'])

    async def test_other_sender_cannot_confirm_and_cancel_never_submits(self):
        await self.process(quoted=True)
        await self.process("确认", identifier="101", sender="other")
        self.workflow.assert_not_awaited()
        await self.process("取消", identifier="102")
        self.workflow.assert_not_awaited()
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))

    async def test_existing_but_unavailable_quote_image_never_falls_back(self):
        self.quote_image.side_effect = ValueError("引用图片下载失败")
        await self.process(quoted=True)
        self.assertIn("引用图片下载失败", self.send.call_args.args[3])
        self.workflow.assert_not_awaited(); self.vision.assert_not_awaited()
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))

    async def test_unknown_department_does_not_accept_model_guessed_city(self):
        self.vision.return_value = {"result": json.dumps({"department": "社会保险事业管理局", "city": "拉萨市"})}
        await self.process(quoted=True)
        self.assertIn("未识别到城市或县区", self.send.call_args.args[3])
        await self.process("确认", identifier="101")
        self.workflow.assert_not_awaited()

    async def test_logged_in_owner_can_confirm_incoming_ticket(self):
        await self.process(quoted=True)
        await self.process("2750 日喀则市", identifier="101", sender="owner")
        self.workflow.assert_awaited_once()
        self.assertEqual("日喀则市", self.mcp.call_args.kwargs["arguments"]["city"])
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))

    async def test_city_ocr_is_used_when_provider_does_not_support_images(self):
        from ai_service import AiServiceError
        self.vision.side_effect = AiServiceError("image input unsupported")
        with patch.object(main, "_smart_reply_image_ocr", AsyncMock(return_value=("申请部门\n山南市社会保险事业管理局", "", "ok"))), \
            patch.object(main.ai_service, "analyze", AsyncMock(side_effect=[ANALYSIS, {"result": json.dumps({"department": "山南市社会保险事业管理局"})}])):
            await self.process(quoted=True)
        self.assertIn("匹配区划：山南市", self.send.call_args.args[3])
        self.workflow.assert_not_awaited()

    async def test_plain_daily_reply_never_matches_or_confirms_city(self):
        await self.process(quoted=True)
        count = self.mcp.await_count
        sent = self.send.await_count
        for index, text in enumerate(("是", "确认", "拉萨市", "改为日喀则市", "取消")):
            await self.process(text, identifier=str(101 + index), mention=False)
        self.assertEqual(count, self.mcp.await_count)
        self.assertEqual(sent, self.send.await_count)
        self.workflow.assert_not_awaited()
        await self.process("是", identifier="106")
        self.workflow.assert_awaited_once()

    async def test_unmentioned_sql_is_ignored(self):
        await self.process(mention=False, quoted=True)
        self.mcp.assert_not_awaited(); self.vision.assert_not_awaited(); self.send.assert_not_awaited()

    async def test_others_need_owner_mention_and_owner_still_needs_air(self):
        await self.process(quoted=True, mention_name="空气")
        self.mcp.assert_not_awaited()
        await self.process(quoted=True, identifier="101", targets=["other"])
        self.mcp.assert_not_awaited()
        await self.process(quoted=True, identifier="102", targets=["owner"])
        self.workflow.assert_not_awaited()
        await self.process("是", identifier="103", sender="owner", mention_name="Aixed", targets=["owner"])
        self.workflow.assert_not_awaited()
        await self.process("是", identifier="104", sender="owner")
        self.workflow.assert_awaited_once()

    async def test_original_sender_can_confirm_by_owner_mention_at_the_end(self):
        await self.process(quoted=True, targets=["owner"])
        await self.process("是 @Aixed", identifier="101", mention=False, targets=["owner"])
        self.workflow.assert_awaited_once()

    async def test_city_confirmation_can_itself_be_a_quote(self):
        await self.process(quoted=True)
        await self.process("是", identifier="101", quoted=True, sender="owner")
        self.workflow.assert_awaited_once()

    def county_response(self):
        area = {"area_name": "昌都市.边坝县", "area_display": "昌都市 / 边坝县", "area_code": "county-code"}
        return {"structured": {"matched": True, "city": "边坝县", **area,
            "supported_cities": ["昌都市"], "supported_areas": [area]}}

    async def test_county_confirmation_passes_verified_full_path_to_workflow(self):
        self.vision.return_value = {"result": json.dumps({"department": "边坝县人力资源和社会保障局"})}
        async def call(**kwargs):
            if kwargs["tool_name"] == "match_data_maintenance_city":
                return self.county_response()
            return await self.workflow(**kwargs)
        self.mcp.side_effect = call
        await self.process(quoted=True)
        prompt = self.send.call_args.args[3]
        self.assertIn("昌都市 / 边坝县", prompt)
        self.assertNotIn("系统支持：", prompt)
        self.assertLess(len(prompt), 120)
        self.workflow.assert_not_awaited()
        await self.process("是", identifier="101", sender="owner")
        self.assertEqual("昌都市.边坝县", self.workflow.call_args.kwargs["arguments"]["city"])
        self.assertEqual(SOURCE, self.workflow.call_args.kwargs["arguments"]["sql_text"])

    async def test_unmatched_image_prompts_for_input_and_explicit_county_runs_immediately(self):
        self.vision.return_value = {"result": json.dumps({"department": "未支持县人社局"})}
        await self.process(quoted=True)
        reply = self.send.call_args.args[3]
        self.assertIn("当前识别/输入：未支持县", reply)
        self.assertIn("请输入城市或县区(示例：@Aixed", reply)
        self.assertNotIn("系统支持：", reply)
        self.assertNotIn("回复“@空气 拉萨市”", reply)
        async def call(**kwargs):
            if kwargs["tool_name"] == "match_data_maintenance_city":
                self.assertEqual("边坝县", kwargs["arguments"]["city_text"])
                return self.county_response()
            return await self.workflow(**kwargs)
        self.mcp.side_effect = call
        await self.process("县区：边坝县", identifier="101", sender="owner")
        self.assertIn("昌都市 / 边坝县", self.send.call_args.args[3])
        self.workflow.assert_awaited_once()

    async def test_unverified_county_code_is_rejected_before_confirmation(self):
        response = self.county_response()
        response["structured"]["area_code"] = "unverified-code"
        self.mcp.return_value = response
        self.mcp.side_effect = None
        await self.process(quoted=True)
        self.assertIn("可核对的系统名称和编码", self.send.call_args.args[3])
        self.workflow.assert_not_awaited()

    async def test_legacy_city_collapsed_from_county_requires_a_new_confirmation(self):
        await self.process(quoted=True)
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        pending["candidate_text"] = "边坝县人社局"
        pending['city_match'].pop('supported_areas')
        OfflineConfirmationStore(self.cache.db_path).save(self.context, pending)
        async def call(**kwargs):
            if kwargs["tool_name"] == "match_data_maintenance_city":
                return self.county_response()
            return await self.workflow(**kwargs)
        self.mcp.side_effect = call
        await self.process("是", identifier="101", sender="owner")
        self.workflow.assert_not_awaited()
        self.assertIn("昌都市 / 边坝县", self.send.call_args.args[3])
        await self.process("是", identifier="102", sender="owner")
        self.workflow.assert_awaited_once()
