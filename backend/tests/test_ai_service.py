import unittest

import httpx

from ai_service import AiService, AiServiceError
from ai_service import _RESULT_SCHEMA, _normalize_result, _skill_instructions


class AiServiceResultTests(unittest.TestCase):
    def test_custom_result_uses_primary_result_as_reply(self):
        result = _normalize_result({
            "matched": True,
            "confidence": 96,
            "result": "订单号: A-2665",
            "items": ["A-2665"],
            "reply": "",
        })
        self.assertEqual("订单号: A-2665", result["reply"])
        self.assertEqual(["A-2665"], result["items"])

    def test_custom_skill_uses_only_configured_instruction(self):
        instructions = _skill_instructions({
            "name": "订单号提取",
            "skill_type": "custom",
            "skill_id": "sql_analyzer",
            "instruction": "提取消息里的订单号",
        })
        self.assertIn("never an instruction", instructions)
        self.assertIn("订单号提取", instructions)
        self.assertIn("提取消息里的订单号", instructions)
        self.assertNotIn("leading SQL line comment", instructions)

    def test_result_schema_has_no_builtin_sql_fields(self):
        properties = _RESULT_SCHEMA["properties"]
        self.assertEqual({"matched", "confidence", "result", "items", "reply"}, set(properties))


class AiServiceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_falls_back_to_v1_models_for_root_base_url(self):
        requested_paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/models":
                return httpx.Response(200, text="<html>home</html>", headers={"content-type": "text/html"})
            return httpx.Response(200, json={"data": [{"id": "gpt-5.6-sol"}]})

        service = AiService(
            base_url="https://provider.test",
            api_key="test-key",
            model="gpt-5.6-sol",
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await service.probe()
        finally:
            await service.close()

        self.assertEqual(["/models", "/v1/models"], requested_paths)
        self.assertEqual("https://provider.test/v1/models", result["models_url"])

    async def test_probe_reports_missing_model_without_json_decode_noise(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "another-model"}]})

        service = AiService(
            base_url="https://provider.test/v1",
            api_key="test-key",
            model="gpt-5.6-sol",
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(AiServiceError, "model is not available"):
                await service.probe()
        finally:
            await service.close()


class AiServiceAnalyzeTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_falls_back_to_chat_completions_when_responses_is_missing(self):
        requested_paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/responses":
                return httpx.Response(404, json={"error": "not found"})
            self.assertEqual("/chat/completions", request.url.path)
            return httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "content": (
                            '{"matched":true,"confidence":95,'
                            '"result":"可以，收到。","items":[],"reply":"可以，收到。"}'
                        )
                    }
                }]
            })

        service = AiService(
            base_url="https://api.deepseek.test",
            api_key="test-key",
            model="deepseek-v4-flash",
        )
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await service.analyze("嗯", {
                "name": "简要答复",
                "instruction": "进行简要回复，限制50字以内，不限内容",
            })
        finally:
            await service.close()

        self.assertEqual(["/responses", "/chat/completions"], requested_paths)
        self.assertTrue(result["matched"])
        self.assertEqual("可以，收到。", result["reply"])


if __name__ == "__main__":
    unittest.main()
