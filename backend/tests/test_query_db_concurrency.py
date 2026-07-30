import asyncio
import unittest
from unittest.mock import patch

import httpx

import wechat_api


class QueryDbConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_concurrency = wechat_api._QUERY_DB_CONCURRENCY
        self.old_wait_timeout = wechat_api._QUERY_DB_POOL_WAIT_TIMEOUT
        self.old_semaphores = wechat_api._query_db_semaphores
        wechat_api._QUERY_DB_CONCURRENCY = 4
        wechat_api._QUERY_DB_POOL_WAIT_TIMEOUT = 0.05
        wechat_api._query_db_semaphores = {}

    async def asyncTearDown(self):
        wechat_api._QUERY_DB_CONCURRENCY = self.old_concurrency
        wechat_api._QUERY_DB_POOL_WAIT_TIMEOUT = self.old_wait_timeout
        wechat_api._query_db_semaphores = self.old_semaphores

    async def test_same_db_caps_at_four_and_returns_empty_on_pool_timeout(self):
        active = 0
        max_active = 0

        async def fake_post(endpoint, json=None, timeout=None, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.2)
                return httpx.Response(200, json={"data": [{"db": json["dbname"]}]})
            finally:
                active -= 1

        with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "_post", fake_post):
            results = await asyncio.gather(*[
                wechat_api.query_db("MicroMsg.db", "select 1")
                for _ in range(5)
            ])

        self.assertEqual(4, max_active)
        self.assertEqual(4, sum(1 for item in results if item.get("data")))
        self.assertEqual(1, sum(1 for item in results if item == {}))

    async def test_different_dbs_have_independent_pools(self):
        active_by_db: dict[str, int] = {}
        max_by_db: dict[str, int] = {}

        async def fake_post(endpoint, json=None, timeout=None, **kwargs):
            dbname = json["dbname"]
            active_by_db[dbname] = active_by_db.get(dbname, 0) + 1
            max_by_db[dbname] = max(max_by_db.get(dbname, 0), active_by_db[dbname])
            try:
                await asyncio.sleep(0.05)
                return httpx.Response(200, json={"data": [{"db": dbname}]})
            finally:
                active_by_db[dbname] -= 1

        with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "_post", fake_post):
            results = await asyncio.gather(
                *[wechat_api.query_db("MSG0.db", "select 1") for _ in range(4)],
                *[wechat_api.query_db("MSG1.db", "select 1") for _ in range(4)],
            )

        self.assertEqual(8, sum(1 for item in results if item.get("data")))
        self.assertEqual({"MSG0.db": 4, "MSG1.db": 4}, max_by_db)


if __name__ == "__main__":
    unittest.main()
