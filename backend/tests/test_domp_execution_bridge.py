import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from domp_execution_bridge import EXECUTION_TOOL, EXECUTION_PROMPT, EXECUTION_REPLY_PREFIX, execution_candidate, execution_target, execute_target, execution_reply_chunks, approval_execution_candidate, approval_execution_targets, approve_execute_targets
from domp_query_bridge import QUERY_TOOL, QUERY_REPLY_PREFIX, TASK_SUMMARY_TOOL, query_arguments, query_tool_call, query_memory_turn, query_reply
from mcp_service import McpServiceError
import test_domp_query_bridge as fixtures

ITEM = {"work_id":"5400000000383894", "work_num":"5400000000383893", "work_name":"41184", "work_status":"2"}
PENDING = {"query_type":"pending_executions", "count":1, "items":[{**ITEM,"identifier":"41184","status":"通过"}]}
EXEC_ANALYSIS = {"matched":True,"confidence":98,"result":'{"action":"execute","identifier":""}'}
BATCH_ANALYSIS = {"matched":True,"confidence":98,"result":'{"action":"approve_execute_all","identifier":""}'}
EXEC_RESULT = {"status":"执行成功", "complete":True,"affected_rows_total":4,"exceptions":[],
    "details":[{"status":"执行成功","affected_rows":4,"elapsed_seconds":0.06,"exception":""},
               {"status":"执行成功","affected_rows":0,"elapsed_seconds":0.01,"exception":""}]}


def history(payload=PENDING):
    args={"query_type":"pending_executions","identifier":""}
    return [query_memory_turn("查一下待执行的任务有多少", args, query_reply(payload), connection_id="local_mcp", payload=payload)]


def approval_history(items=None, count=None):
    items = items or [{**ITEM, "identifier": "41184", "status": "待审批"}]
    payload = {"query_type": "pending_approvals", "count": len(items) if count is None else count, "items": items}
    return [query_memory_turn("查一下待审核的数据运维单", {"query_type": "pending_approvals", "identifier": ""},
        query_reply(payload), connection_id="local_mcp", payload=payload)]


class ExecutionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_batch_approval_execution_requires_explicit_action_and_verified_complete_targets(self):
        for source in ("都审核执行一下", "全部审批并执行", "把上面的单子都审核后执行",
                       "能不能帮我把上面的单子都审核后执行"):
            self.assertTrue(approval_execution_candidate(source), source)
            self.assertTrue(execution_candidate(source), source)
            targets = approval_execution_targets(source, BATCH_ANALYSIS, approval_history())
            self.assertEqual(ITEM["work_num"], targets[0]["work_num"])
        for source in ("都执行一下", "都审核一下", "可以都审核执行吗", "不要都审核执行", "例如都审核执行一下"):
            self.assertFalse(approval_execution_candidate(source), source)
        with self.assertRaises(ValueError):
            approval_execution_targets("都审核执行一下", BATCH_ANALYSIS, [])
        with self.assertRaises(ValueError):
            approval_execution_targets("都审核执行一下", BATCH_ANALYSIS, approval_history(count=21))
        with self.assertRaises(ValueError):
            approval_execution_targets("都审核执行一下", EXEC_ANALYSIS, approval_history())
        legacy = approval_history(); legacy[0].pop("approval_targets")
        self.assertEqual(ITEM["work_num"], approval_execution_targets("都审核执行一下", BATCH_ANALYSIS, legacy)[0]["work_num"])

    async def test_batch_revalidates_then_approves_and_executes_each_target_once(self):
        second = {"work_id": "5400000000383900", "work_num": "5400000000383899", "work_name": "41185", "identifier": "41185", "work_status": "0"}
        items = [{**ITEM, "identifier": "41184"}, second]
        targets = approval_execution_targets("都审核执行一下", BATCH_ANALYSIS, approval_history(items))
        calls = []
        async def call(name, args):
            calls.append((name, args))
            if name == QUERY_TOOL:
                return {"structured": {"query_type": "pending_approvals", "count": 2, "items": items}}
            if name == "approve_data_maintenance":
                return {"structured": {**args, "status": "审核通过"}}
            return {"structured": EXEC_RESULT if args["work_num"] == ITEM["work_num"] else
                {"status": "执行成功", "complete": True, "affected_rows_total": 0, "details": [], "exceptions": []}}
        reply = await approve_execute_targets(targets, call)
        self.assertEqual([QUERY_TOOL, "approve_data_maintenance", EXECUTION_TOOL,
                          "approve_data_maintenance", EXECUTION_TOOL], [name for name, _ in calls])
        self.assertIn("批量目标：2 条", reply)
        self.assertIn("影响条数合计：4", reply)
        self.assertIn("影响条数合计：0", reply)

    async def test_batch_stale_or_dry_run_never_writes_and_uncertain_approval_is_not_executed(self):
        targets = approval_execution_targets("都审核执行一下", BATCH_ANALYSIS, approval_history())
        calls = []
        async def stale(name, args):
            calls.append(name); return {"structured": {"query_type": "pending_approvals", "count": 0, "items": []}}
        with self.assertRaises(ValueError):
            await approve_execute_targets(targets, stale)
        self.assertEqual([QUERY_TOOL], calls)
        calls.clear()
        async def present(name, args):
            calls.append(name)
            if name == QUERY_TOOL:
                return {"structured": {"query_type": "pending_approvals", "count": 1, "items": [{**ITEM, "identifier": "41184"}]}}
            raise McpServiceError("审核超时")
        self.assertIn("仅查询", await approve_execute_targets(targets, present, dry_run=True))
        self.assertEqual([QUERY_TOOL], calls)
        calls.clear()
        reply = await approve_execute_targets(targets, present)
        self.assertEqual([QUERY_TOOL, "approve_data_maintenance"], calls)
        self.assertIn("审核：待核对", reply)
        self.assertIn("执行：未开始", reply)

    def test_commands_queries_and_legacy_stable_target(self):
        for source in ("执行下并告诉我结果","执行一下","帮我执行41184","41184 执行下","把这单执行一下",
                       "兄弟，帮我执行下 41184","老师，麻烦把数据运维单 41184 执行一下", "@助手 帮我执行下41184",
                       "41184，麻烦执行一下", "能不能帮我执行41184", "可以帮我把41184执行一下吗"):
            self.assertTrue(execution_candidate(source),source)
        for source in ("41233 执行了吗，结果如何","执行结果如何","待执行呢","不要执行41184","如果41184执行下","明天执行41184",
                       "兄弟，帮我查一下41184执行结果","兄弟，帮我看看41184有没有执行","兄弟，不要帮我执行41184", "他说帮我执行41184",
                       "我已经执行41184", "41184能不能执行", "‘执行41184’这句话能识别吗"):
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
            if name=="approve_data_maintenance":return {"structured":{**kwargs["arguments"],"status":"审核通过"}}
            if name==EXECUTION_TOOL:return {"structured":EXEC_RESULT}
            return {"structured":self.query_result}
        self.main.ai_service.analyze=analyze;self.main.mcp_service.call_tool=call_tool

    async def test_reported_owner_two_steps_route_to_single_execution_and_reply_effects(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"pending_executions","identifier":""}'}
        self.query_result=PENDING
        await self.process(self.message(msg="@空气 " + ("查一下待执行的任务有多少"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        await self.process(self.message(id="101",msg="@空气 " + ("执行下并告诉我结果"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.assertEqual([QUERY_TOOL,"list_data_maintenance_executions",EXECUTION_TOOL],[c["tool_name"] for c in self.calls])
        self.assertEqual({k:ITEM[k] for k in ("work_id","work_num")},self.calls[-1]["arguments"])
        self.assertEqual((self.chat,self.owner),self.send.call_args.args[:2])
        reply=self.send.call_args.args[3]
        self.assertIn("执行结果：执行成功",reply);self.assertIn("影响条数合计：4",reply)
        self.assertIn("SQL 2：执行成功，影响 0 条",reply);self.assertIn("异常详情：无",reply)
        await self.process(self.message(id="102",msg="@空气 " + (reply),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.assertEqual(3,len(self.calls))

    async def test_pending_approval_followup_routes_to_pinned_batch_approval_and_execution(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"pending_approvals","identifier":""}'}
        self.query_result={"query_type":"pending_approvals","count":1,"items":[{**ITEM,"identifier":"41184","status":"待审批"}]}
        await self.process(self.message(msg="@空气 " + ("查一下待审核的数据运维单"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.exec_analysis=BATCH_ANALYSIS
        await self.process(self.message(id="151",msg="@空气 " + ("都审核执行一下"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.assertEqual([QUERY_TOOL, QUERY_TOOL, "approve_data_maintenance", EXECUTION_TOOL],
            [call["tool_name"] for call in self.calls])
        reply=self.send.call_args.args[3]
        self.assertIn("审核：审核通过",reply)
        self.assertIn("执行：执行成功",reply)
        self.assertIn("影响条数合计：4",reply)

    async def test_allowed_sender_yesterday_query_then_batch_is_supported(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"pending_maintenance","identifier":""}'}
        yesterday=(date.today()-timedelta(days=1)).isoformat()
        approval={"query_type":"pending_approvals","date_from":yesterday,"date_to":yesterday,"date_field":"申请时间",
            "count":1,"items":[{**ITEM,"identifier":"41184","status":"待审批"}]}
        execution={"query_type":"pending_executions","date_from":yesterday,"date_to":yesterday,"date_field":"审批通过时间","count":0,"items":[]}
        async def call_tool(**kwargs):
            self.calls.append(kwargs); name=kwargs["tool_name"]
            if name==QUERY_TOOL:
                kind=kwargs["arguments"]["query_type"]
                return {"structured":approval if kind=="pending_approvals" else execution}
            if name=="approve_data_maintenance":return {"structured":{**kwargs["arguments"],"status":"审核通过"}}
            if name==EXECUTION_TOOL:return {"structured":EXEC_RESULT}
            raise AssertionError(name)
        self.main.mcp_service.call_tool=call_tool
        await self.process(self.message(msg="昨天的还有没审核执行的不@Aixed"))
        self.assertTrue(all(call["arguments"].get("date_from")==yesterday for call in self.calls[:2]))
        self.exec_analysis=BATCH_ANALYSIS
        await self.process(self.message(id="181",msg="都审核执行一下@Aixed"))
        self.assertEqual([QUERY_TOOL,QUERY_TOOL,QUERY_TOOL,"approve_data_maintenance",EXECUTION_TOOL],
            [call["tool_name"] for call in self.calls])
        self.assertIn("审核：审核通过",self.send.call_args.args[3])
        self.assertIn("执行：执行成功",self.send.call_args.args[3])

    async def test_reported_greeting_command_passes_owner_gate_and_validates_current_ticket(self):
        self.exec_analysis={**EXEC_ANALYSIS,"result":'{"action":"execute","identifier":"41184"}'}
        for index,source in enumerate(("兄弟，帮我执行下 41184", "老师，麻烦把数据运维单 41184 执行一下", "@助手 帮我执行下41184")):
            await self.process(self.message(id=str(200+index),msg="@空气 " + (source),fromid=self.owner,isSender=1,sendorrecv="1", atuserlist=[self.owner]))
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
        await self.process(self.message(id="102",fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.assertEqual([],self.calls) # Owner's raw SQL is still excluded.

    async def test_expired_context_does_not_execute(self):
        session=self.cache.append_query_turn(self.chat,self.owner,history()[0],owner_wxid=self.owner)
        with patch("sqlite_cache.time.time",return_value=session["created_at"]+7*86400):
            await self.process(self.message(msg="@空气 " + ("执行下"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
        self.assertEqual([],self.calls);self.assertIn("超过一周",self.send.call_args.args[3])

    async def test_status_question_stays_readonly_with_execution_agent_enabled(self):
        self.query_analysis={**fixtures.ANALYZED,"result":'{"query_type":"execution","identifier":"41233"}'}
        await self.process(self.message(msg="@空气 " + ("41233 执行了吗，结果如何"),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
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
            await self.process(self.message(id=str(index),msg="@空气 " + (source),fromid=self.owner,isSender=1, atuserlist=[self.owner]))
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
