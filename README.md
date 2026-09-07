# labor-contract-extract

人事行政专属后端。当前生产职责是劳动合同状态同步、到期提醒，以及“人事行政助手”的卡片长连接回调。云端 OCR 端点已停用；`#合同识别` 由本机 `hr_local_bridge.py` 接收并执行。

## 端点
- `GET /health`
- `POST /sync-status?dry_run=true` — 只预览员工状态变化
- `POST /remind?dry_run=true` — 只预览到期提醒与收件人解析，不发消息
- `POST /scan` — 已禁用，不再做云端 OCR

## 规则（按附件槽路由）
- 试用期劳动合同附件 → 签约公司/职称/起止/期限/试用期到期/底薪 → 员工状态=试用期
- 转正劳动合同附件 → 上述 + KPI基数 → 员工状态=转正（覆盖）
- 续签协议附件 → 签约公司/协议起止/期限 → 续签状态=已续签完成
- 其余附件槽（保密协议/入职承诺书/离职文件/培训协议）仅归档，不提取
- 幂等：已解析的 file_token 记录在「_解析记录(系统)」，不重复解析

## 环境变量
- `HR_FEISHU_APP_ID` / `HR_FEISHU_APP_SECRET`（人事行政助手；只放 Zeabur 密钥环境）
- `DASHSCOPE_KEY`（通义千问 Qwen-VL）
- `CONTRACT_APP_TOKEN` / `CONTRACT_TABLE_ID`（默认已指向劳动合同台账）
- `OCR_MAX_PAGES`（默认 7）
- `REMINDER_RECIPIENT_NAMES`（默认仅高泳昭、吴晓丹、潘志聪；从当前 App 读取的人员字段解析 open_id）
- `HR_CARD_ACTIONS`（默认只允许 `hr_r7_verify`）
- `HR_CALLBACK_ENABLED`（云端默认 `1`；迁移到本机长连接后设为 `0`，确保同一 App 只有一个消费者）

由 n8n 每日 09:30 BJ 依次调用 `/sync-status` 与 `/remind`。人员状态读取失败或员工缺少飞书账号时，`/sync-status` 返回 HTTP 424、列出失败对象且不写任何状态；任一提醒目标发送失败时 `/remind` 返回 HTTP 502，并返回脱敏后的飞书错误码。每个目标按“北京时间日期 + 接收类型 + 接收 ID”生成飞书原生 `uuid`，防止 1 小时内的工作流重试重复发送。卡片回调使用官方 `lark-channel-sdk` 常驻长连接，拒绝非 `hr_` 或未列入允许清单的动作。

R9 起服务只接受 `HR_FEISHU_APP_ID/HR_FEISHU_APP_SECRET`，不再读取通用旧 App 凭据。回滚通过上一代码版本和受控重新部署完成，不通过静默切换到通用 App。

## `#合同识别` 本机入口

`hr_local_bridge.py` 使用同一组 `HR_FEISHU_APP_ID/HR_FEISHU_APP_SECRET` 建立唯一长连接，接收私聊指令或群内 @机器人后的严格指令：

- `#合同识别`：扫描全部未解析附件。
- `#合同识别 姓名`：只处理该员工。

它只读取试用期、转正、续签三个附件槽；成功后回填 `AI核对状态=待核对` 并登记 file_token，失败不会登记为已解析。云端 `HR_CALLBACK_ENABLED=0` 后才能启动本机消费者，避免同一 App 事件被两个消费者抢收。
