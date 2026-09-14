import tempfile
from pathlib import Path
import unittest
import asyncio
from unittest.mock import patch

from sqlite_cache import SqliteMessageCache
from domp_query_bridge import query_memory_turn, query_arguments


class QueryConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "cache.sqlite3")
        self.cache = SqliteMessageCache(self.path)
        self.turn = query_memory_turn("查41198任务处理情况", {"query_type":"task_processing","identifier":"41198"}, "转现场处理", message_id="one", connection_id="local_mcp")

    def test_persistent_sessions_isolate_owner_chat_and_sender(self):
        session = self.cache.append_query_turn("group", "alice", self.turn, owner_wxid="owner", now=100)
        restarted = SqliteMessageCache(self.path)
        self.assertEqual(session, restarted.get_query_conversation("group", "alice", owner_wxid="owner", now=101))
        for owner,chat,sender in (("other","group","alice"),("owner","other","alice"),("owner","group","bob")):
            with self.subTest(owner=owner,chat=chat,sender=sender):
                self.assertIsNone(restarted.get_query_conversation(chat,sender,owner_wxid=owner,now=101))

    def test_hard_week_expiry_does_not_slide_when_active_and_creates_new_id(self):
        first = self.cache.append_query_turn("group","alice",self.turn,owner_wxid="owner",now=100)
        self.cache.append_query_turn("group","alice",{**self.turn,"message_id":"two"},owner_wxid="owner",now=100+6*86400)
        self.assertIsNotNone(self.cache.get_query_conversation("group","alice",owner_wxid="owner",now=100+7*86400-1))
        self.assertIsNone(self.cache.get_query_conversation("group","alice",owner_wxid="owner",now=100+7*86400))
        second = self.cache.append_query_turn("group","alice",self.turn,owner_wxid="owner",now=100+7*86400)
        self.assertNotEqual(first["session_id"],second["session_id"])
        self.assertEqual(1,len(second["turns"]))

    def test_context_is_bounded_and_duplicate_callback_does_not_append(self):
        for index in range(20):
            self.cache.append_query_turn("group","alice",{**self.turn,"message_id":str(index)},owner_wxid="owner",now=100+index)
        self.cache.append_query_turn("group","alice",{**self.turn,"message_id":"19"},owner_wxid="owner",now=121)
        session=self.cache.get_query_conversation("group","alice",owner_wxid="owner",now=122)
        self.assertEqual(12,len(session["turns"]))
        self.assertEqual("8",session["turns"][0]["message_id"])
        bounded=query_memory_turn("x"*10000,self.turn["arguments"],"y"*10000)
        self.assertEqual(1500,len(bounded["message"]))
        self.assertEqual(1500,len(bounded["answer"]))

    def test_context_target_is_validated_and_latest_turn_wins(self):
        analysis={"result":'{"query_type":"execution","identifier":"41198"}'}
        self.assertEqual({"query_type":"execution","identifier":"41198"},query_arguments("它的执行结果呢",analysis,[self.turn]))
        wrong={"result":'{"query_type":"execution","identifier":"41199"}'}
        with self.assertRaises(ValueError):query_arguments("它的执行结果呢",wrong,[self.turn])
        with self.assertRaises(ValueError):query_arguments("它的执行结果呢",analysis,[])
        counts=query_memory_turn("有哪些待审批",{"query_type":"pending_approvals","identifier":""},"0条")
        with self.assertRaises(ValueError):query_arguments("它的执行结果呢",analysis,[self.turn,counts])
        changed=query_arguments("那41199呢",wrong,[self.turn])
        self.assertEqual({"query_type":"task_processing","identifier":"41199"},changed)


class ConversationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_sender_is_fifo_while_other_sender_runs_independently(self):
        import main
        entered = asyncio.Event()
        release = asyncio.Event()
        other_entered = asyncio.Event()
        started = []
        async def fake_process(**kwargs):
            msg = kwargs['message']
            started.append(msg['id'])
            if msg['id'] == 'first':
                entered.set()
                await release.wait()
            if msg['id'] == 'other':
                other_entered.set()
        def process(sender,identifier):
            return asyncio.create_task(main._process_smart_reply_message(owner_wxid='owner',agent_id='agent',self_wxid='owner',chat_id='group',message={'fromid':sender,'id':identifier}))
        with patch.object(main,'_process_smart_reply_message_unlocked',fake_process):
            first = process('alice','first')
            await asyncio.wait_for(entered.wait(),2)
            second = process('alice','second')
            other = process('bob','other')
            try:
                await asyncio.wait_for(other_entered.wait(),2)
                self.assertNotIn('second',started)
            finally:
                release.set()
                await asyncio.gather(first,second,other)
        self.assertLess(started.index('first'),started.index('second'))
        self.assertEqual({},main._smart_reply_conversation_locks)


if __name__ == "__main__":unittest.main()
