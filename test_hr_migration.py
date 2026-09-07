import asyncio
import datetime
import importlib
import io
import json
import os
import types
import unittest
from unittest import mock

import app
import hr_callback
import hr_local_bridge
import hr_readonly


class RecipientNamespaceTests(unittest.TestCase):
    def test_resolves_all_recipients_from_current_app_person_fields(self):
        rows = [
            {"fields": {"员工姓名": [{"text": "高泳昭"}], "员工(飞书账号)": [{"id": "ou_new_gao"}]}},
            {"fields": {"员工姓名": [{"text": "吴晓丹"}], "员工(飞书账号)": [{"id": "ou_new_wu"}]}},
            {"fields": {"员工姓名": [{"text": "潘志聪"}], "员工(飞书账号)": [{"id": "ou_new_frankie"}]}},
        ]
        resolved, missing = app.resolve_recipient_open_ids(rows)
        self.assertEqual(resolved, {
            "高泳昭": "ou_new_gao", "吴晓丹": "ou_new_wu", "潘志聪": "ou_new_frankie"
        })
        self.assertEqual(missing, [])

    def test_never_falls_back_to_legacy_open_ids(self):
        resolved, missing = app.resolve_recipient_open_ids([], ("高泳昭",))
        self.assertEqual(resolved, {})
        self.assertEqual(missing, ["高泳昭"])


class ReminderSafetyTests(unittest.TestCase):
    def test_req_rejects_http_200_with_nonzero_business_code(self):
        response = io.BytesIO(json.dumps({"code": 99991672, "msg": "missing scope"}).encode())
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "remote_api_error:99991672"):
                app._req("GET", "https://example.invalid")

    def test_parsed_marker_is_written_only_after_all_business_fields(self):
        calls = []

        def fake_update(_token, _record_id, fields):
            calls.append(fields)
            if "AI核对状态" in fields:
                raise RuntimeError("select_write_failed")

        with mock.patch.object(app, "update_row", side_effect=fake_update):
            with self.assertRaisesRegex(RuntimeError, "select_write_failed"):
                app.update_row_stable("token", "rec", {
                    "备注": "已提取",
                    "AI核对状态": "待核对",
                    "_解析记录(系统)": '["file_test"]',
                })
        self.assertEqual(calls, [{"备注": "已提取"}, {"AI核对状态": "待核对"}])

    def test_send_msg_uses_stable_per_target_idempotency_uuid(self):
        with mock.patch.object(app, "_req", return_value={"data": {"message_id": "om_test"}}) as req:
            app.send_msg("token", "oc_test", "chat_id", "body", "2026-09-04:HR群")
            first_body = req.call_args.kwargs["body"]
            app.send_msg("token", "oc_test", "chat_id", "body", "2026-09-04:HR群")
            second_body = req.call_args.kwargs["body"]
        self.assertEqual(first_body["uuid"], second_body["uuid"])
        self.assertNotIn("uuid=", req.call_args.args[1])

    def test_remind_reports_partial_delivery_failure(self):
        due = int((datetime.datetime.now(app.TZ) + datetime.timedelta(days=1)).timestamp() * 1000)
        rows = [
            {"fields": {"员工姓名": [{"text": "高泳昭"}], "员工(飞书账号)": [{"id": "ou_gao"}],
                        "员工状态": "转正", "合同类型": "固定期限", "合同到期日期": due}},
            {"fields": {"员工姓名": [{"text": "吴晓丹"}], "员工(飞书账号)": [{"id": "ou_wu"}]}},
            {"fields": {"员工姓名": [{"text": "潘志聪"}], "员工(飞书账号)": [{"id": "ou_pan"}]}},
        ]

        def fake_send(_token, receive_id, _id_type, _body, _key):
            if receive_id == "ou_wu":
                raise RuntimeError("unavailable")
            return {"data": {"message_id": "om_test"}}

        with mock.patch.object(app, "feishu_token", return_value="token"), \
             mock.patch.object(app, "list_rows", return_value=rows), \
             mock.patch.object(app, "send_msg", side_effect=fake_send):
            result = app.remind(dry_run=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["sent_count"], 3)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(result["delivery"]), 4)

    def test_sync_status_fails_closed_when_contact_lookup_fails(self):
        rows = [{"record_id": "rec_test", "fields": {
            "员工姓名": [{"text": "测试员工"}],
            "员工(飞书账号)": [{"id": "ou_test"}],
            "员工状态": "",
            "试用期劳动合同附件": [{"file_token": "file_test"}],
        }}]
        with mock.patch.object(app, "feishu_token", return_value="token"), \
             mock.patch.object(app, "list_rows", return_value=rows), \
             mock.patch.object(app, "get_user", side_effect=PermissionError("missing scope")), \
             mock.patch.object(app, "update_row_stable") as update:
            result = app.sync_status(dry_run=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["contact_lookup_failures"], 1)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["contact_failures"][0]["record_id"], "rec_test")
        update.assert_not_called()

    def test_sync_status_fails_closed_when_open_id_is_missing(self):
        rows = [{"record_id": "rec_missing", "fields": {
            "员工姓名": [{"text": "无账号员工"}],
            "员工状态": "",
            "试用期劳动合同附件": [{"file_token": "file_test"}],
        }}]
        with mock.patch.object(app, "feishu_token", return_value="token"), \
             mock.patch.object(app, "list_rows", return_value=rows), \
             mock.patch.object(app, "update_row_stable") as update:
            result = app.sync_status(dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["contact_failures"][0]["error"], "missing_open_id")
        update.assert_not_called()

    def test_send_msg_surfaces_feishu_business_error(self):
        with mock.patch.object(app, "_req", return_value={"code": 230013, "msg": "Bot unavailable"}):
            with self.assertRaisesRegex(app.FeishuAPIError, "230013"):
                app.send_msg("token", "ou_test", "open_id", "body", "key")

    def test_http_endpoints_surface_business_failure(self):
        with mock.patch.object(app, "remind", return_value={"ok": False, "failed_count": 1}):
            self.assertEqual(app.remind_ep(dry_run=False).status_code, 502)
        with mock.patch.object(app, "sync_status", return_value={"ok": False, "contact_lookup_failures": 1}):
            self.assertEqual(app.sync_status_ep(dry_run=False).status_code, 424)


class CardCallbackTests(unittest.TestCase):
    def setUp(self):
        hr_callback.STATE.update(last_update_ok=None, error=None, last_action=None)

    def test_allowlisted_hr_action_updates_original_card(self):
        class Channel:
            def __init__(self): self.updated = []
            async def update_card(self, message_id, card):
                self.updated.append((message_id, card))
                return types.SimpleNamespace(success=True)
        channel = Channel()
        event = types.SimpleNamespace(
            message_id="om_test",
            operator=types.SimpleNamespace(open_id="ou_operator"),
            action=types.SimpleNamespace(value={"action": "hr_r7_verify"}),
        )
        asyncio.run(hr_callback.handle_card_action(event, channel))
        self.assertEqual(channel.updated[0][0], "om_test")
        self.assertEqual(channel.updated[0][1]["header"]["template"], "green")
        self.assertTrue(hr_callback.STATE["last_update_ok"])

    def test_unknown_action_is_rejected_without_update(self):
        class Channel:
            async def update_card(self, *_):
                raise AssertionError("must not update")
        event = types.SimpleNamespace(
            message_id="om_test",
            operator=types.SimpleNamespace(open_id="ou_operator"),
            action=types.SimpleNamespace(value={"action": "finance_approve"}),
        )
        asyncio.run(hr_callback.handle_card_action(event, Channel()))
        self.assertEqual(hr_callback.STATE["error"], "action_not_allowed")

    def test_cloud_callback_can_be_disabled_for_local_cutover(self):
        with mock.patch.dict(os.environ, {"HR_CALLBACK_ENABLED": "0"}, clear=True), \
             mock.patch.object(hr_callback, "_THREAD", None):
            hr_callback.STATE.update(enabled=True, connection="connected", error=None)
            hr_callback.start()
        self.assertFalse(hr_callback.STATE["enabled"])
        self.assertEqual(hr_callback.STATE["connection"], "disabled")
        self.assertEqual(hr_callback.STATE["error"], "disabled_by_config")

    def test_cloud_callback_is_fail_closed_when_flag_is_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(hr_callback, "_THREAD", None):
            hr_callback.STATE.update(enabled=True, connection="connected", error=None)
            hr_callback.start()
        self.assertFalse(hr_callback.STATE["enabled"])
        self.assertEqual(hr_callback.STATE["connection"], "disabled")
        self.assertEqual(hr_callback.STATE["error"], "disabled_by_config")


class LocalContractBridgeTests(unittest.TestCase):
    def test_command_parser_is_strict_and_normalizes_fullwidth(self):
        self.assertEqual(hr_local_bridge.parse_contract_command("＃合同识别　测试员工"), "测试员工")
        self.assertEqual(hr_local_bridge.parse_contract_command("#合同识别"), "")
        self.assertIsNone(hr_local_bridge.parse_contract_command("帮我#合同识别"))

    def test_group_message_requires_bot_mention(self):
        msg = types.SimpleNamespace(
            sender_is_bot=False, chat_type="group", mentioned_bot=False,
            body_text="#合同识别 测试员工", content_text="#合同识别 测试员工",
        )
        channel = types.SimpleNamespace(reply=mock.AsyncMock())
        asyncio.run(hr_local_bridge.handle_message(msg, channel))
        channel.reply.assert_not_awaited()

    def test_named_command_runs_once_and_replies(self):
        msg = types.SimpleNamespace(
            sender_is_bot=False, chat_type="p2p", mentioned_bot=False,
            body_text="#合同识别 测试员工", content_text="#合同识别 测试员工",
        )
        channel = types.SimpleNamespace(reply=mock.AsyncMock())
        with mock.patch.object(hr_local_bridge, "execute_contract_command", return_value="已完成") as execute:
            asyncio.run(hr_local_bridge.handle_message(msg, channel))
        execute.assert_called_once_with("测试员工")
        self.assertEqual(channel.reply.await_count, 2)


class HRReadonlyCommandTests(unittest.TestCase):
    def test_parses_roster_job_and_attendance_commands(self):
        self.assertEqual(hr_readonly.parse_command("＃花名册　人事部"),
                         {"kind": "roster", "keyword": "人事部"})
        self.assertEqual(hr_readonly.parse_command("#岗位查询 独立站运营专员"),
                         {"kind": "job", "keyword": "独立站运营专员"})
        self.assertEqual(hr_readonly.parse_command("#考勤查询 测试员工 2026-09-01 2026-09-07"), {
            "kind": "attendance", "name": "测试员工",
            "start": "2026-09-01", "end": "2026-09-07",
        })
        self.assertIsNone(hr_readonly.parse_command("帮我查花名册"))

    def test_roster_projects_only_business_fields_and_filters_job(self):
        employees = [{"system_fields": {
            "name": "测试员工", "department_id": "od-test",
            "job": {"name": "人事专员"}, "status": 2,
            "mobile": "must-not-leak", "id_number": "must-not-leak",
        }}]
        with mock.patch.object(hr_readonly, "feishu_token", return_value="token"), \
             mock.patch.object(hr_readonly, "list_employees", return_value=employees), \
             mock.patch.object(hr_readonly, "_department_names", return_value={"od-test": "人事部"}):
            result = hr_readonly.query_roster("人事", by_job=True)
        self.assertEqual(result["rows"], [{
            "name": "测试员工", "job": "人事专员", "department": "人事部", "status": "在职"
        }])
        self.assertNotIn("mobile", json.dumps(result))
        self.assertNotIn("id_number", json.dumps(result))

    def test_attendance_summary_omits_sensitive_raw_details(self):
        employee = {"user_id": "employee-test", "system_fields": {"name": "测试员工"}}
        api_result = {"code": 0, "data": {"user_task_results": [{"records": [{
            "check_in_result": "Late", "check_out_result": "Normal",
            "check_in_record": {"location_name": "must-not-leak", "photo_urls": ["must-not-leak"]},
        }]}], "invalid_user_ids": [], "unauthorized_user_ids": []}}
        with mock.patch.object(hr_readonly, "feishu_token", return_value="token"), \
             mock.patch.object(hr_readonly, "list_employees", return_value=[employee]), \
             mock.patch.object(hr_readonly, "_request_json", return_value=api_result):
            result = hr_readonly.query_attendance("测试员工", "2026-09-01", "2026-09-01")
        self.assertEqual(result["late_days"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("location_name", serialized)
        self.assertNotIn("photo_urls", serialized)

    def test_system_check_counts_as_normal_day(self):
        employee = {"user_id": "employee-test", "system_fields": {"name": "测试员工"}}
        api_result = {"code": 0, "data": {"user_task_results": [{"records": [{
            "check_in_result": "SystemCheck", "check_out_result": "SystemCheck",
        }]}], "invalid_user_ids": [], "unauthorized_user_ids": []}}
        with mock.patch.object(hr_readonly, "feishu_token", return_value="token"), \
             mock.patch.object(hr_readonly, "list_employees", return_value=[employee]), \
             mock.patch.object(hr_readonly, "_request_json", return_value=api_result):
            result = hr_readonly.query_attendance("测试员工", "2026-09-01", "2026-09-01")
        self.assertEqual(result["scheduled_days"], 1)
        self.assertEqual(result["normal_days"], 1)

    def test_readonly_command_runs_once_and_replies_without_progress_message(self):
        msg = types.SimpleNamespace(
            sender_is_bot=False, chat_type="p2p", mentioned_bot=False,
            body_text="#花名册 人事部", content_text="#花名册 人事部",
        )
        channel = types.SimpleNamespace(reply=mock.AsyncMock())
        with mock.patch.object(hr_local_bridge, "execute_readonly_command", return_value="只读结果") as execute:
            asyncio.run(hr_local_bridge.handle_message(msg, channel))
        execute.assert_called_once_with({"kind": "roster", "keyword": "人事部"})
        channel.reply.assert_awaited_once_with(msg, "只读结果")

    def test_readonly_errors_are_explained_without_internal_error_names(self):
        msg = types.SimpleNamespace(
            sender_is_bot=False, chat_type="p2p", mentioned_bot=False,
            body_text="#考勤查询 测试员工 2026/09/01", content_text="#考勤查询 测试员工 2026/09/01",
        )
        channel = types.SimpleNamespace(reply=mock.AsyncMock())
        with mock.patch.object(
            hr_local_bridge, "execute_readonly_command",
            side_effect=hr_readonly.HRReadonlyError("invalid_date_format"),
        ):
            asyncio.run(hr_local_bridge.handle_message(msg, channel))
        reply = channel.reply.await_args.args[1]
        self.assertIn("YYYY-MM-DD", reply)
        self.assertNotIn("HRReadonlyError", reply)


class DedicatedCredentialTests(unittest.TestCase):
    def test_app_does_not_accept_legacy_app_credentials(self):
        env = {
            "FEISHU_APP_ID": "legacy-id",
            "FEISHU_APP_SECRET": "legacy-secret",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            reloaded = importlib.reload(app)
            self.assertEqual(reloaded.FEISHU_APP_ID, "")
            self.assertEqual(reloaded.FEISHU_APP_SECRET, "")
        importlib.reload(app)

    def test_callback_does_not_start_with_legacy_app_credentials(self):
        env = {
            "HR_CALLBACK_ENABLED": "1",
            "FEISHU_APP_ID": "legacy-id",
            "FEISHU_APP_SECRET": "legacy-secret",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(hr_callback, "_THREAD", None):
            hr_callback.STATE.update(enabled=True, connection="connected", error=None)
            hr_callback.start()
        self.assertFalse(hr_callback.STATE["enabled"])
        self.assertEqual(hr_callback.STATE["connection"], "disabled")
        self.assertEqual(hr_callback.STATE["error"], "missing_credentials")


if __name__ == "__main__":
    unittest.main()
