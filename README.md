# labor-contract-extract

劳动合同 OCR 抽取服务。扫描飞书「劳动合同台账」中新上传的合同附件，用 Qwen-VL OCR 提取关键字段并回填（标记"待核对"）。全云端，不依赖本地。

## 端点
- `GET /health`
- `POST /scan?dry_run=true` — 只返回将回填的字段，不写表（验证用）
- `POST /scan` — 真回填（dry_run 默认 false）；可选 `?limit=N`

## 规则（按附件槽路由）
- 试用期劳动合同附件 → 签约公司/职称/起止/期限/试用期到期/底薪 → 员工状态=试用期
- 转正劳动合同附件 → 上述 + KPI基数 → 员工状态=转正（覆盖）
- 续签协议附件 → 签约公司/协议起止/期限 → 续签状态=已续签完成
- 其余附件槽（保密协议/入职承诺书/离职文件/培训协议）仅归档，不提取
- 幂等：已解析的 file_token 记录在「_解析记录(系统)」，不重复解析

## 环境变量
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（聪哥1号，台账协作者）
- `DASHSCOPE_KEY`（通义千问 Qwen-VL）
- `CONTRACT_APP_TOKEN` / `CONTRACT_TABLE_ID`（默认已指向劳动合同台账）
- `OCR_MAX_PAGES`（默认 7）

由 n8n 每小时 cron 调 `POST /scan`。
