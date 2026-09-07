"""Stable long-connection consumer for the 人事行政助手 App.

Only explicitly allowlisted ``hr_`` card actions are handled. The consumer
never reads or writes HR Base records; business handlers must be added as
separate reviewed changes.
"""
import asyncio
import os
import threading
import time


ALLOWED_ACTIONS = frozenset(
    x.strip() for x in os.environ.get("HR_CARD_ACTIONS", "hr_r7_verify").split(",")
    if x.strip()
)

STATE = {
    "enabled": False,
    "connection": "idle",
    "started_at": None,
    "last_event_at": None,
    "last_action": None,
    "last_update_ok": None,
    "error": None,
}

_CHANNEL = None
_THREAD = None


def action_name(event):
    value = getattr(getattr(event, "action", None), "value", None)
    if isinstance(value, dict):
        return str(value.get("action") or "")
    return ""


def completed_card():
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "🟢 [HR·P3] 人事行政助手稳定回调 · 已通过"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**验证结果**\n云端常驻消费者已收到按钮操作，并由同一 App 更新原卡。"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "本次未读取或写入 HR Base，未触发员工消息、审批或考勤写入。"}]},
        ],
    }


async def handle_card_action(event, channel):
    name = action_name(event)
    message_id = getattr(event, "message_id", "") or ""
    operator = getattr(getattr(event, "operator", None), "open_id", "") or ""
    STATE["last_event_at"] = int(time.time())
    STATE["last_action"] = name or "unknown"
    if not name.startswith("hr_") or name not in ALLOWED_ACTIONS:
        STATE["last_update_ok"] = False
        STATE["error"] = "action_not_allowed"
        return
    if not message_id or not operator:
        STATE["last_update_ok"] = False
        STATE["error"] = "missing_event_identity"
        return
    result = await channel.update_card(message_id, completed_card())
    ok = bool(getattr(result, "success", False))
    STATE["last_update_ok"] = ok
    STATE["error"] = None if ok else "card_update_failed"


def _run(app_id, app_secret):
    global _CHANNEL
    from lark_channel import Events, FeishuChannel, SecurityConfig

    async def main():
        global _CHANNEL
        channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            security=SecurityConfig(mode="strict"),
        )
        _CHANNEL = channel
        channel.on(Events.CARD_ACTION, lambda event: handle_card_action(event, channel))
        channel.on(Events.RECONNECTED, lambda *_: STATE.update(connection="connected", error=None))
        channel.on(Events.ERROR, lambda err: STATE.update(connection="error", error=type(err).__name__))
        STATE.update(enabled=True, connection="connecting", started_at=int(time.time()), error=None)
        await channel.connect()

    try:
        asyncio.run(main())
    except Exception as ex:
        STATE.update(connection="error", error=type(ex).__name__)


def start():
    global _THREAD
    if os.environ.get("HR_CALLBACK_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        STATE.update(enabled=False, connection="disabled", error="disabled_by_config")
        return
    app_id = os.environ.get("HR_FEISHU_APP_ID", "")
    app_secret = os.environ.get("HR_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        STATE.update(enabled=False, connection="disabled", error="missing_credentials")
        return
    if _THREAD and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_run, args=(app_id, app_secret), daemon=True,
                               name="hr-card-callback")
    _THREAD.start()


def snapshot():
    out = dict(STATE)
    if _CHANNEL is not None:
        try:
            conn = _CHANNEL.connection_snapshot()
            out["connection"] = conn.state
            out["ready"] = conn.ready
            out["reconnect_attempts"] = conn.reconnect_attempts
        except Exception:
            pass
    return out
