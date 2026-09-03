import asyncio
import types
import unittest

import app
import hr_callback


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


if __name__ == "__main__":
    unittest.main()
