import json
import unittest

from domp_bridge import workflow_arguments
from domp_offline_bridge import offline_analysis_task
from domp_sql_extraction import SQL_EXTRACTION_PROMPT, extracted_sql_source
from domp_unified_bridge import unified_route


SQL = "UPDATE x.AB01 SET note='a;@Aixed', aab034='540421' -- @昵称\nWHERE aab001=540499335983;"


class SqlExtractionTests(unittest.TestCase):
    def test_ai_separates_interleaved_chat_while_preserving_sql_bytes(self):
        second = "UPDATE x.AC01 SET aab034='540421' WHERE aac001 IN (SELECT aac001 FROM x.AC02 WHERE aab001=540499335983);"
        source = "--xzsb-2800-1\n帮忙处理单位转移\n" + SQL + "\n第一段处理完单位，下面是人员\n" + second + "\n @Aixed \n工布江达县"
        analysis = {"matched": True, "confidence": 98, "result": json.dumps({
            "intent": "sql_workflow", "identifier": "2800-1", "sql_purpose": "单位及人员转移",
            "risk_level": "medium", "risk_notes": [], "sql_blocks": [SQL, second]})}
        tool, routed = unified_route(source, analysis)
        self.assertEqual("process_data_maintenance", tool)
        args = workflow_arguments(source, routed, {"_source_context": {"message_id": "source-1"}})
        self.assertEqual("2800-1", args["identifier"])
        self.assertEqual(SQL + "\n" + second, args["sql_text"])
        self.assertEqual(args["request_id"], workflow_arguments(source, routed, {"_source_context": {"message_id": "source-1"}})["request_id"])

    def test_rewrites_reordering_duplicates_and_omissions_stop_before_mcp(self):
        second = "DELETE FROM x.AC02 WHERE aac001=2;"
        source = SQL + "\n" + second
        for blocks in ([SQL.replace("540421", "540422"), second], [second, SQL], [SQL],
                       [SQL, SQL, second], [SQL[:SQL.index("WHERE")].strip(), second], [], "sql"):
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                extracted_sql_source(source, blocks)

    def test_unsafe_sql_in_chat_cannot_be_silently_discarded(self):
        for extra in ("DROP TABLE x.AB01;", "COMMIT;", "CALL x.proc();", "WITH c AS (SELECT 1) SELECT * FROM c;"):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "遗漏"):
                extracted_sql_source(SQL + "\n聊天\n" + extra, [SQL])

    def test_no_semicolon_is_allowed_only_at_the_end_of_the_source(self):
        sql = "UPDATE x.AB01 SET aab034='540421' WHERE aab001=1"
        self.assertEqual(sql, extracted_sql_source("--xzsb-2800-1\n" + sql, [sql]))
        with self.assertRaisesRegex(ValueError, "遗漏后续条件"):
            extracted_sql_source(sql + " AND aab034='540100'", [sql])
        with self.assertRaises(ValueError):
            extracted_sql_source(sql + "\n@Aixed", [sql])

    def test_sql_keywords_inside_values_and_comments_are_not_omissions(self):
        source = "-- UPDATE 的业务说明\n" + SQL + "\n-- DROP 不要执行\n@Aixed"
        self.assertEqual(SQL, extracted_sql_source(source, [SQL]))

    def test_saved_custom_prompts_also_get_ai_extraction_instructions(self):
        for task in ({"skill_id": "domp_unified_agent", "instruction": "自定义规则"},
                     {"mcp_tool_name": "process_data_maintenance", "instruction": "自定义规则"}):
            updated = offline_analysis_task(task, "--xzsb-42157\n" + SQL)
            self.assertTrue(updated["instruction"].startswith("自定义规则"))
            self.assertIn(SQL_EXTRACTION_PROMPT, updated["instruction"])
        self.assertEqual({"instruction": "普通任务"}, offline_analysis_task({"instruction": "普通任务"}, SQL))


if __name__ == "__main__":
    unittest.main()
