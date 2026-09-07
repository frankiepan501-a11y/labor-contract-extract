"""Local long-connection consumer for 人事行政助手 contract OCR commands.

The target App owns message reception, Base reads/writes, and replies. OCR stays
on the Windows host because the configured DashScope endpoint is not reliable
from the Tokyo cloud runtime.
"""
from __future__ import annotations

import asyncio
import ctypes
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata

logging.basicConfig(level=logging.ERROR)

from lark_channel import Events, FeishuChannel, SecurityConfig


OCR_SCRIPT = Path(os.environ.get(
    "LABOR_CONTRACT_SCRIPT",
    str(Path.home() / "scripts" / "labor_contract_extract.py"),
))
_spec = importlib.util.spec_from_file_location("labor_contract_extract_runtime", OCR_SCRIPT)
if _spec is None or _spec.loader is None:
    raise RuntimeError("labor_contract_script_unloadable")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)
from hr_callback import handle_card_action


COMMAND_RE = re.compile(r"^#合同识别(?:\s+(.+))?$", re.UNICODE)
RUN_LOCK = threading.Lock()
STATUS_PATH = Path(os.environ.get(
    "HR_CONTRACT_STATUS_PATH",
    str(Path.home() / ".claude-to-im" / "runtime" / "hr-contract-bridge-status.json"),
))
STATUS = {"state": "starting", "pid": os.getpid(), "heartbeat_at": 0, "last_command_at": 0, "error": None}


def write_status(**changes) -> None:
    STATUS.update(changes, heartbeat_at=int(time.time()))
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATUS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(STATUS, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATUS_PATH)


def parse_contract_command(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = re.sub(r"[\u200B-\u200D\uFEFF]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    match = COMMAND_RE.fullmatch(normalized)
    if not match:
        return None
    return (match.group(1) or "").strip()


def format_result(name: str, scan_result: dict, status_result: dict) -> str:
    lines = [app.format_scan_result(scan_result), "", app.format_status_sync_result(status_result)]
    if not status_result.get("ok", True):
        lines += ["", "员工状态同步未执行写入，请检查飞书人员字段后再试。"]
    return "\n".join(lines)


def execute_contract_command(name: str) -> str:
    scan_result = app.scan(dry_run=False, name_filter=name or None)
    status_result = app.sync_status(dry_run=False, name_filter=name or None)
    return format_result(name, scan_result, status_result)


async def handle_message(msg, channel) -> None:
    if getattr(msg, "sender_is_bot", False):
        return
    if getattr(msg, "chat_type", "") in {"group", "topic"} and not getattr(msg, "mentioned_bot", False):
        return
    text = getattr(msg, "body_text", "") or getattr(msg, "content_text", "") or ""
    name = parse_contract_command(text)
    if name is None:
        return
    write_status(last_command_at=int(time.time()))
    if not RUN_LOCK.acquire(blocking=False):
        await channel.reply(msg, "合同识别正在处理中，请等待上一项完成后再试。")
        return
    try:
        target = name or "全员未解析附件"
        await channel.reply(msg, f"已收到 #合同识别，目标：{target}。开始读取劳动合同台账；只处理未解析附件。")
        try:
            result = await asyncio.to_thread(execute_contract_command, name)
        except Exception as exc:
            await channel.reply(msg, f"合同识别失败（{type(exc).__name__}）。本次未标记附件为已解析，请稍后重试或联系管理员。")
            return
        await channel.reply(msg, result)
    finally:
        RUN_LOCK.release()


def acquire_single_instance():
    if os.name != "nt":
        return None
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\HRContractRecognitionBridge")
    if not handle or ctypes.windll.kernel32.GetLastError() == 183:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        raise RuntimeError("hr_contract_bridge_already_running")
    return handle


async def run() -> None:
    app_id = os.environ.get("HR_FEISHU_APP_ID", "")
    app_secret = os.environ.get("HR_FEISHU_APP_SECRET", "")
    dashscope_key = os.environ.get("DASHSCOPE_KEY", "") or os.environ.get("LABOR_CONTRACT_DASHSCOPE_KEY", "")
    if not app_id or not app_secret or not dashscope_key:
        raise RuntimeError("missing_hr_contract_credentials")
    channel = FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        security=SecurityConfig(mode="strict"),
    )
    channel.on(Events.MESSAGE, lambda msg: handle_message(msg, channel))
    channel.on(Events.CARD_ACTION, lambda event: handle_card_action(event, channel))
    channel.on(Events.ERROR, lambda err: write_status(state="error", error=type(err).__name__))
    await channel.connect_until_ready(timeout=30)
    write_status(state="connected", error=None)
    while True:
        await asyncio.sleep(30)
        snapshot = channel.connection_snapshot()
        write_status(state=snapshot.state, error=None if snapshot.ready else "connection_not_ready")


def main() -> int:
    mutex = acquire_single_instance()
    try:
        try:
            asyncio.run(run())
        except Exception as exc:
            write_status(state="stopped", error=type(exc).__name__)
            raise
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
