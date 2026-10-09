import unittest
from unittest.mock import patch
import payroll_notify as pn


class PayrollNotifyTests(unittest.TestCase):
    def test_rejects_nonfinancial_numbers_and_invalid_recipient(self):
        for bad in (True, float('nan'), float('inf')):
            p = self.payload(); p['employees'][0]['net'] = bad
            with self.assertRaises(ValueError): pn.notify(p)
        p = self.payload(); p['recipients'] = [{}]
        with self.assertRaises(ValueError): pn.notify(p)

    def test_preflight_failure_has_error_code_without_sending(self):
        with patch.object(pn.hr_readonly, 'feishu_token', side_effect=RuntimeError('remote_api_error:99991663')):
            result = pn.notify(self.payload())
        self.assertFalse(result['ok']); self.assertEqual(result['receipts'], [])
        self.assertIn('99991663', result['error'])

    def payload(self):
        return {'month': '2026/09', 'dry_run': False,
                'employees': [{'name': '甲', 'sheetToken': 'ValidBook123', 'grade': 'D', 'net': 100, 'bonus': 20}]}

    def people(self):
        return [{'user_id': 'ou_' + str(i), 'system_fields': {'name': name, 'status': 2}}
                for i, name in enumerate(pn.RECIPIENTS)]

    def test_preflight_no_send_and_same_app_roster(self):
        p = self.payload(); p['dry_run'] = True
        with patch.object(pn.hr_readonly, 'feishu_token', return_value='hr-token'), \
             patch.object(pn.hr_readonly, 'list_employees', return_value=self.people()) as roster, \
             patch.object(pn, '_send') as send:
            result = pn.notify(p)
        self.assertTrue(result['ok']); send.assert_not_called()
        roster.assert_called_once_with('hr-token', user_id_type='open_id', statuses=(2, 4))

    def test_departed_recipient_never_sent_and_missing_stops_before_send(self):
        people = self.people(); people[1]['system_fields']['status'] = 5
        with patch.object(pn.hr_readonly, 'feishu_token', return_value='hr-token'), \
             patch.object(pn.hr_readonly, 'list_employees', return_value=people), \
             patch.object(pn, '_send') as send:
            result = pn.notify(self.payload())
        self.assertFalse(result['ok']); send.assert_not_called()
        self.assertEqual(result['missing'], [pn.RECIPIENTS[1]])

    def test_partial_failure_preserves_other_receipts_and_no_added_recipient(self):
        with patch.object(pn.hr_readonly, 'feishu_token', return_value='hr-token'), \
             patch.object(pn.hr_readonly, 'list_employees', return_value=self.people()), \
             patch.object(pn, '_send', side_effect=['om_one', RuntimeError('remote_api_error:230013'), 'om_three']):
            result = pn.notify(self.payload())
        self.assertFalse(result['ok'])
        self.assertEqual([r.get('message_id') for r in result['receipts']], ['om_one', None, 'om_three'])
        self.assertEqual([r['name'] for r in result['receipts']], list(pn.RECIPIENTS))
        self.assertIn('230013', result['receipts'][1]['error'])

    def test_targeted_retry_and_stable_uuid(self):
        p = self.payload(); p['recipients'] = [pn.RECIPIENTS[1]]
        with patch.object(pn.hr_readonly, 'feishu_token', return_value='hr-token'), \
             patch.object(pn.hr_readonly, 'list_employees', return_value=self.people()), \
             patch.object(pn, '_send', return_value='om_one') as send:
            pn.notify(p); pn.notify(p)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        p['recipients'] = ['莫莉莉']
        with self.assertRaises(ValueError): pn.notify(p)

    def test_transport_uuid_in_body_and_message_id_required(self):
        with patch.object(pn.hr_readonly, '_request_json', return_value={'data': {'message_id': 'om_test'}}) as req:
            self.assertEqual(pn._send('token', 'ou_target', {}, 'stable-key'), 'om_test')
        self.assertEqual(len(req.call_args.kwargs['body']['uuid']), 32)
        self.assertNotIn('uuid=', req.call_args.args[1])
        with patch.object(pn.hr_readonly, '_request_json', return_value={'data': {}}):
            with self.assertRaises(RuntimeError): pn._send('token', 'ou_target', {}, 'stable-key')

if __name__ == '__main__': unittest.main()
