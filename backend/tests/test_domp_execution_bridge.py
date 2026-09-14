import json
import unittest
from unittest.mock import patch

from domp_execution_bridge import EXECUTION_TOOL, EXECUTION_PROMPT, EXECUTION_REPLY_PREFIX, execution_candidate, execution_target, execute_target, execution_reply_chunks
from domp_query_bridge import QUERY_TOOL, QUERY_REPLY_PREFIX, TASK_SUMMARY_TOOL, query_arguments, query_tool_call, query_memory_turn, query_reply
from mcp_service import McpServiceError
import test_domp_query_bridge as fixtures

ITEM = {"work_id":"5400000000383894", "work_num":"5400000000383893", "work_name":"41184", "work_status":"2"}
PENDING = {"query_type":"pending_executions", "count":1, "items":[{**ITEM,"identifier":"41184","status":"通过"}]}
EXEC_ANALYSIS = {"matched":True,"confidence":98,"result":'{"action":"execute","identifier":""}'}
EXEC_RESULT = {"status":"执行成功", "complete":True,"affected_rows_total":4,"exceptions":[],
    "details":[{"status":"执行成功","affected_rows":4,"elapsed_seconds":0.06,"exception":""},
               {"status":"执行成功","affected_rows":0,"elapsed_seconds":0.01,"exception":""}]}


def history(payload=PENDING):
    args={"query_type":"pending_executions","identifier":""}
    return [query_memory_turn("查一下待执行的任务有多少", args, query_reply(payload), connection_id="local_mcp", payload=payload)]


class ExecutionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_commands_queries_and_legacy_stable_target(self):
        for source in ("执行下并告诉我结果","执行一下","帮我执行41184","41184 执行下","把这单执行一下",
                       "兄弟，帮我执行下 41184","老师，麻烦把数据运维单 41184 执行一下", "@助手 帮我执行下41184", "41184，麻烦执行一下"):
            self.assertTrue(execution_candidate(source),source)
        for source in ("41233 执行了吗，结果如何","执行结果如何","待执行呢","不要执行41184","如果41184执行下","明天执行41184",
                       "兄弟，帮我查一下41184执行结果","兄弟，帮我看看41184有没有执行","兄弟，不要帮我执行41184", "他说帮我执行41184", "我已经执行41184"):
            self.assertFalse(execution_candidate(source),source)
        target=execution_target("执行下并告诉我结果",EXEC_ANALYSIS,history())
        self.assertEqual(ITEM["work_num"],target["work_num"])
        legacy=history(); legacy[0].pop("execution_targets")
        self.assertEqual(target["work_num"],execution_target("执行下",EXEC_ANALYSIS,legacy)["work_num"])
        for turns in ([],history({**PENDING,"count":0,"items":[]}),history({**PENDING,"count":2})):
            with self.assertRaises(ValueError):execution_target("执行下",EXEC_ANALYSIS,turns)

    async def test_old_target_disappearing_does_not_execute_new_singleton(self):
        calls=[]
        async def call(name,args):
            calls.append((name,args))
            return {"structured":{"items":[{**ITEM,"work_num":"5400000000000001"}]} if name.startswith("list_") else {"status":"未找到"}}
        reply,_=await execute_target(execution_target("执行下",EXEC_ANALYSIS,history()),call)
        self.assertEqual(["list_data_maintenance_executions",QUERY_TOOL],[c[0] for c in calls])
        self.assertEqual(ITEM["work_num"],calls[-1][1]["identifier"])
        self.assertIn("本次未发起执行",reply)

    async def test_timeout_only_reads_never_retries_write_and_unknown_is_honest(self):
        calls=[]
        async def call(name,args):
            calls.append(name)
            if name==EXECUTION_TOOL:raise McpServiceError("response timeout")
            return {"structured":{"items":[ITEM]} if name.startswith("list_") else {"status":"未执行","complete":False}}
        reply,_=await execute_target(execution_target("执行下",EXEC_ANALYSIS,history()),call)
        self.assertEqual(["list_data_maintenance_executions",EXECUTION_TOOL,"get_data_maintenance_execution_result"],calls)
        self.assertIn("执行结果：待核对",reply); self.assertIn("影响条数合计：未知",reply)
        self.assertIn("response timeout",reply)

    async def test_skill_test_readonly_and_duplicate_names_stop(self):
        calls=[]
        async def call(name,args):
            calls.append(name); return {"structured":{"items":[ITEM]}}
        target={"identifier":"41184","work_num":""}
        reply,_=await execute_target(target,call,dry_run=True)
        self.assertEqual(["list_data_maintenance_executions"],calls);self.assertIn("未发起执行",reply)
        async def duplicate(name,args):return {"structured":{"items":[ITEM,{**ITEM,"work_num":"5400000000000002"}]}}
        with self.assertRaises(ValueError):await execute_target(target,duplicate)

    def test_explicit_current_identifier_overrides_context_and_analysis_is_checked(self):
        analysis={**EXEC_ANALYSIS,"result":'{"action":"execute","identifier":"41199"}'}
        self.assertEqual({"identifier":"41199","work_num":""},execution_target("执行41199",analysis,history()))
        with self.assertRaises(ValueError):execution_target("执行41184",analysis,history())
        with self.assertRaises(ValueError):execution_target("执行41184和41199",analysis,history())
        reply=EXECUTION_REPLY_PREFIX+"\n异常详情："+"长异常"*1000
        chunks=execution_reply_chunks(reply)
        self.assertTrue(all(len(x)<=1800 and not execution_candidate(x) for x in chunks))


class ExecutionGroupTests(unittest.IsolatedAsyncioTestCase):
    process=fixtures.QueryGroupTests.process
    message=fixtures.QueryGroupTests.message

    async def asyncSetUp(self):
        await fixtures.QueryGroupTests.asyncSetUp(self)
        row=self.cache.get_smart_reply_config(self.chat,owner_wxid=self.owner)
        row["ai_tasks"].append({**row["ai_tasks"][0],"id":"execute","name":"执行 Agent","instruction":EXECUTION_PROMPT,"mcp_tool_name":EXECUTION_TOOL})
        self.cache.upsert_smart_reply_config(row,owner_wxid=self.owner)
        self.exec_analysis=EXEC_ANALYSIS
        async def analyze(content,task):
            self.analyzed_messages.append(content)
            return self.exec_analysis if task.get("mcp_tool_name")==EXECUTION_TOOL else self.query_analysis
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            name=kwargs["tool_name"]
            if name=="list_data_maintenance_executions":return {"structured":{"items":[ITEM]}}
            if name==EXECUTION_TOOL:return {"structured":EXEC_RESULT}
            return {"structured":self.query_result}
        self.main.ai_service.analyze=analyze;self.main.mcp_service.call_tool=call_tool

    async def test_reported_owner_two_steps_route_to_single_execution_and_reply_effects(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"pending_executions","identifier":""}'}
        self.query_result=PENDING
        await self.process(self.message(msg="查一下待执行的任务有多少",fromid=self.owner,isSender=1))
        await self.process(self.message(id="101",msg="执行下并告诉我结果",fromid=self.owner,isSender=1))
        self.assertEqual([QUERY_TOOL,"list_data_maintenance_executions",EXECUTION_TOOL],[c["tool_name"] for c in self.calls])
        self.assertEqual({k:ITEM[k] for k in ("work_id","work_num")},self.calls[-1]["arguments"])
        self.assertEqual((self.chat,self.owner),self.send.call_args.args[:2])
        reply=self.send.call_args.args[3]
        self.assertIn("执行结果：执行成功",reply);self.assertIn("影响条数合计：4",reply)
        self.assertIn("SQL 2：执行成功，影响 0 条",reply);self.assertIn("异常详情：无",reply)
        await self.process(self.message(id="102",msg=reply,fromid=self.owner,isSender=1))
        self.assertEqual(3,len(self.calls))

    async def test_reported_greeting_command_passes_owner_gate_and_validates_current_ticket(self):
        self.exec_analysis={**EXEC_ANALYSIS,"result":'{"action":"execute","identifier":"41184"}'}
        for index,source in enumerate(("兄弟，帮我执行下 41184", "老师，麻烦把数据运维单 41184 执行一下", "@助手 帮我执行下41184")):
            await self.process(self.message(id=str(200+index),msg=source,fromid=self.owner,isSender=1,sendorrecv="1"))
        self.assertEqual(["list_data_maintenance_executions",EXECUTION_TOOL]*3,[c["tool_name"] for c in self.calls])
        self.assertTrue(all(c["arguments"]=={k:ITEM[k] for k in ("work_id","work_num")} for c in self.calls if c["tool_name"]==EXECUTION_TOOL))
        self.assertEqual((self.chat,self.owner),self.send.call_args.args[:2])
        self.assertIn("影响条数合计：4",self.send.call_args.args[3])

    async def test_other_sender_cannot_inherit_owner_target_and_unlisted_is_blocked(self):
        self.cache.append_query_turn(self.chat,self.owner,history()[0],owner_wxid=self.owner)
        await self.process(self.message(msg="执行下并告诉我结果"))
        self.assertEqual([],self.calls);self.assertIn("没有明确待执行",self.send.call_args.args[3])
        await self.process(self.message(id="101",msg="执行41184",fromid="not_allowed"))
        self.assertEqual([],self.calls)
        await self.process(self.message(id="102",fromid=self.owner,isSender=1))
        self.assertEqual([],self.calls) # Owner's raw SQL is still excluded.

    async def test_expired_context_does_not_execute(self):
        session=self.cache.append_query_turn(self.chat,self.owner,history()[0],owner_wxid=self.owner)
        with patch("sqlite_cache.time.time",return_value=session["created_at"]+7*86400):
            await self.process(self.message(msg="执行下",fromid=self.owner,isSender=1))
        self.assertEqual([],self.calls);self.assertIn("超过一周",self.send.call_args.args[3])

    async def test_status_question_stays_readonly_with_execution_agent_enabled(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"execution","identifier":"41233"}'}
        await self.process(self.message(msg="41233 执行了吗，结果如何",fromid=self.owner,isSender=1))
        self.assertEqual([QUERY_TOOL],[c["tool_name"] for c in self.calls])


class TaskSummaryGroupTests(unittest.IsolatedAsyncioTestCase):
    process=fixtures.QueryGroupTests.process
    message=fixtures.QueryGroupTests.message
    async def asyncSetUp(self):
        await fixtures.QueryGroupTests.asyncSetUp(self)
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            return {"structured":self.query_result}
        self.main.mcp_service.call_tool=call_tool

    async def test_pending_and_today_details_refresh_readonly_tools(self):
        for index,(source,kind) in enumerate((("当前有多少待处理的任务","pending_tasks"),("今天分配了多少任务","today_assignments"),("详情呢","today_assignments"))):
            self.query_analysis={**fixtures.ANALYZED,"result":json.dumps({"query_type":kind,"identifier":""})}
            self.query_result={"query_type":kind,"date":"2026-09-14","count":1,"items":[{"identifier":"xzsb-41198","summary":"测试", "assignee":"张三","assigned_at":"2026-09-14T10:00:00+08:00","result":"已提交并从待处理列表移除"}],"coverage_note":"历史范围"}
            await self.process(self.message(id=str(index),msg=source,fromid=self.owner,isSender=1))
        self.assertEqual([TASK_SUMMARY_TOOL]*3,[c["tool_name"] for c in self.calls])
        self.assertEqual(["pending_tasks","today_assignments","today_assignments"],[c["arguments"]["query_type"] for c in self.calls])
        self.assertIn("指派人员：张三",self.send.call_args.args[3]); self.assertIn("历史范围",self.send.call_args.args[3])

    def test_pagination_from_verified_history_only(self):
        args={"query_type":"today_assignments","identifier":"","offset":0,"limit":20}
        turn=query_memory_turn("今天分配了多少任务",args,"结果",payload={"offset":0,"shown_count":20,"has_more":True})
        analysis={"result":'{"query_type":"today_assignments","identifier":"","offset":999}'}
        actual=query_arguments("下一页",analysis,[turn])
        self.assertEqual((TASK_SUMMARY_TOOL,{"query_type":"today_assignments","offset":20,"limit":20}),query_tool_call(actual))
        with self.assertRaises(ValueError):query_arguments("下一页",analysis,[])


if __name__=="__main__":unittest.main()
