# 人事行政助手

Existing service handles contract reminders and minimum HR facts. Protected internal endpoints use HR_INTERNAL_API_TOKEN; Feishu identity is exclusively HR_FEISHU_APP_ID/SECRET. Cloud callback remains disabled; the established local consumer owns callbacks.

Payroll calculation and Base writes belong to the existing n8n workflow. The payroll notification endpoint only accepts the generated month and summary, resolves existing active recipients in the HR App namespace, and returns per-recipient message receipts. It has no wage database permissions.
