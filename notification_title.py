"""Credential-free PAY subset of ~/scripts/_lib/feishu_title.py:format_title.

Only formatting is shared; sender and explicit recipients belong to HR.
"""
def format_title(biz, level, title, suffix=''):
    if biz != 'PAY' or level != 'P2':
        raise ValueError('unsupported_payroll_notification_title')
    head = f'🟡 [{biz}·{level}] {title}'
    return f'{head} · {suffix}' if suffix else head
