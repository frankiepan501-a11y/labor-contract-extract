"""Existing Amazon payroll notifications, sent by 人事行政助手 only.

No payroll database access, no callbacks and no new recipients. Native Feishu
UUID protects retries for one hour; later recovery must target failed names only.
"""
import hashlib
import json
import re
import urllib.error

import hr_readonly
from notification_title import format_title

RECIPIENTS = ('潘志聪', '高泳昭', '吴晓丹')
FOLDER = 'https://u1wpma3xuhr.feishu.cn/drive/folder/M926finxFlGWwAdWiuXcgFdRnwh'


def _send(token, open_id, card, key):
    result = hr_readonly._request_json(
        'POST', hr_readonly.FEISHU + '/im/v1/messages?receive_id_type=open_id', token,
        body={'receive_id': open_id, 'msg_type': 'interactive',
              'content': json.dumps(card, ensure_ascii=False),
              'uuid': hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]})
    message_id = (result.get('data') or {}).get('message_id')
    if not message_id:
        raise RuntimeError('message_id_missing')
    return message_id


def notify(payload):
    month = str(payload.get('month') or '')
    employees = payload.get('employees') or []
    wanted = payload.get('recipients', list(RECIPIENTS))
    dry_run = payload.get('dry_run', True)
    if (not re.fullmatch(r'20\d\d/(0[1-9]|1[0-2])', month)
            or not isinstance(dry_run, bool)
            or not isinstance(wanted, list) or not wanted
            or len(wanted) != len(set(wanted)) or set(wanted) - set(RECIPIENTS)
            or not isinstance(employees, list) or not 1 <= len(employees) <= 100):
        raise ValueError('invalid_payroll_notification')
    for emp in employees:
        if (not isinstance(emp, dict) or not isinstance(emp.get('name'), str)
                or not 1 <= len(emp['name']) <= 50
                or not re.fullmatch(r'[A-Za-z0-9]{6,80}', str(emp.get('sheetToken') or ''))):
            raise ValueError('invalid_payroll_employee')
        for field in ('net', 'bonus'):
            if not isinstance(emp.get(field), (int, float)):
                raise ValueError('invalid_payroll_amount')
        if emp.get('grade') not in ('A', 'B', 'C', 'D', 'E', '免考核'):
            raise ValueError('invalid_payroll_grade')

    token = hr_readonly.feishu_token()
    people = hr_readonly.list_employees(token, user_id_type='open_id', statuses=(2, 4))
    matches = {name: [] for name in wanted}
    for person in people:
        fields = person.get('system_fields') or {}
        name = fields.get('name') or ''
        if name in matches and fields.get('status') in (2, 4) and person.get('user_id'):
            matches[name].append(person['user_id'])
    missing = [name for name in wanted if len(set(matches[name])) != 1]
    result = {'ok': not missing, 'dry_run': dry_run, 'month': month,
              'sender_app_id': 'cli_aa12f52686f89bd5', 'missing': missing,
              'receipts': [], 'recipients': wanted}
    if missing:
        return result
    event_key = 'amazon-payroll:' + month + ':' + ','.join(sorted(e['sheetToken'] for e in employees))
    title = format_title('PAY', 'P2', '工资单已生成', month)
    for name in wanted:
        open_id = matches[name][0]
        if name == '高泳昭':
            text = f'{month} 工资单已生成，请核实\n共计：{len(employees)} 人\n' + '、'.join(e['name'] for e in employees)
        else:
            text = '\n'.join(f"{e['name']} | 绩效：{e['grade']} | 实发：{e['net']:.2f} | 年终奖基数：{e['bonus']:.2f}" for e in employees)
        text += '\n状态：待人事最终核对。'
        card = {'config': {'wide_screen_mode': True},
                'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue' if name == '高泳昭' else 'green'},
                'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': text}},
                             {'tag': 'action', 'actions': [{'tag': 'button', 'text': {'tag': 'plain_text', 'content': '前往核实'},
                                                           'type': 'primary', 'url': FOLDER}]}]}
        receipt = {'name': name, 'open_id': open_id, 'status': 'preview' if dry_run else 'failed'}
        if dry_run:
            receipt['card'] = card
        else:
            try:
                receipt['message_id'] = _send(token, open_id, card, event_key + ':' + open_id)
                receipt['status'] = 'sent'
            except urllib.error.HTTPError as exc:
                try:
                    error = json.loads(exc.read().decode('utf-8'))
                    receipt['error'] = f'http={exc.code} feishu_code={error.get("code")}'
                except Exception:
                    receipt['error'] = f'http={exc.code}'
            except Exception as exc:
                # Only emit an error class and known numeric code, never request headers.
                code = re.search(r'(?:error:|code=)(\d+)', str(exc))
                receipt['error'] = type(exc).__name__ + (':' + code.group(1) if code else '')
        result['receipts'].append(receipt)
    result['ok'] = all(r['status'] in ('preview', 'sent') for r in result['receipts'])
    return result
