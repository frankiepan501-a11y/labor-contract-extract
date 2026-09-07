"""Read-only HR roster, job-title and attendance queries for 人事行政助手.

Only the minimum business fields are returned to chat. Raw roster and attendance
payloads are never logged or persisted by this module.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request


FEISHU = "https://open.feishu.cn/open-apis"
ROSTER_RE = re.compile(r"^#花名册(?:\s+(.+))?$")
JOB_RE = re.compile(r"^#岗位查询(?:\s+(.+))?$")
ATTENDANCE_RE = re.compile(r"^#考勤查询(?:\s+(.+))?$")
STATUS_LABELS = {1: "待入职", 2: "在职", 3: "已取消入职", 4: "待离职", 5: "已离职"}


class HRReadonlyError(RuntimeError):
    pass


ERROR_MESSAGES = {
    "invalid_date_format": "日期格式不正确，请使用 YYYY-MM-DD。",
    "invalid_date_order": "开始日期不能晚于结束日期。",
    "date_range_over_31_days": "一次最多查询连续 31 天，请缩短日期范围。",
    "future_date_not_allowed": "结束日期不能晚于今天。",
    "employee_not_found": "未在飞书人事当前花名册中找到该员工。",
    "employee_name_ambiguous": "姓名匹配到多人，请输入完整姓名。",
    "attendance_user_unauthorized": "该员工不在“人事行政助手”的可查询范围内。",
    "attendance_user_invalid": "飞书考勤无法识别该员工，请核对花名册与考勤账号。",
}


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"[\u200B-\u200D\uFEFF]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_command(text: str) -> dict | None:
    value = _normalize(text)
    match = ROSTER_RE.fullmatch(value)
    if match:
        return {"kind": "roster", "keyword": (match.group(1) or "").strip()}
    match = JOB_RE.fullmatch(value)
    if match:
        return {"kind": "job", "keyword": (match.group(1) or "").strip()}
    match = ATTENDANCE_RE.fullmatch(value)
    if not match:
        return None
    args = (match.group(1) or "").split()
    if len(args) not in (1, 2, 3):
        return {"kind": "invalid_attendance"}
    result = {"kind": "attendance", "name": args[0]}
    if len(args) >= 2:
        result["start"] = args[1]
    if len(args) == 3:
        result["end"] = args[2]
    return result


def _request_json(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    response = urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers, method=method), timeout=45
    )
    result = json.load(response)
    if result.get("code", 0) != 0:
        raise HRReadonlyError(f"feishu_business_error:{result.get('code')}")
    return result


def feishu_token() -> str:
    app_id = os.environ.get("HR_FEISHU_APP_ID", "")
    app_secret = os.environ.get("HR_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise HRReadonlyError("missing_hr_credentials")
    result = _request_json("POST", f"{FEISHU}/auth/v3/tenant_access_token/internal", body={
        "app_id": app_id, "app_secret": app_secret,
    })
    return result["tenant_access_token"]


def list_employees(token: str, user_id_type: str = "open_id") -> list[dict]:
    items: list[dict] = []
    page_token = ""
    while True:
        params = [("view", "full"), ("status", "2"), ("status", "4"),
                  ("user_id_type", user_id_type), ("page_size", "100")]
        if page_token:
            params.append(("page_token", page_token))
        result = _request_json("GET", f"{FEISHU}/ehr/v1/employees?{urllib.parse.urlencode(params)}", token)
        page = result.get("data") or {}
        items.extend(page.get("items") or [])
        if not page.get("has_more"):
            break
        page_token = page.get("page_token") or ""
        if not page_token:
            raise HRReadonlyError("ehr_pagination_token_missing")
    return items


def _department_names(token: str, employees: list[dict]) -> dict[str, str]:
    department_ids = {
        (item.get("system_fields") or {}).get("department_id")
        for item in employees if (item.get("system_fields") or {}).get("department_id")
    }
    names = {}
    for department_id in department_ids:
        result = _request_json(
            "GET",
            f"{FEISHU}/contact/v3/departments/{urllib.parse.quote(department_id)}"
            "?department_id_type=open_department_id", token,
        )
        department = (result.get("data") or {}).get("department") or {}
        names[department_id] = department.get("name") or "未命名部门"
    return names


def query_roster(keyword: str = "", by_job: bool = False) -> dict:
    token = feishu_token()
    employees = list_employees(token)
    departments = _department_names(token, employees)
    needle = _normalize(keyword).casefold()
    rows = []
    for item in employees:
        fields = item.get("system_fields") or {}
        row = {
            "name": fields.get("name") or "未命名",
            "job": (fields.get("job") or {}).get("name") or "未填写职务",
            "department": departments.get(fields.get("department_id"), "未填写部门"),
            "status": STATUS_LABELS.get(fields.get("status"), "未知"),
        }
        haystack = row["job"] if by_job else " ".join(row.values())
        if not needle or needle in haystack.casefold():
            rows.append(row)
    rows.sort(key=lambda row: (row["department"], row["job"], row["name"]))
    return {"ok": True, "source": "feishu_ehr_live", "count": len(rows), "rows": rows}


def format_roster(result: dict, by_job: bool = False, keyword: str = "") -> str:
    title = "岗位查询" if by_job else "实时花名册"
    suffix = f"（关键词：{keyword}）" if keyword else ""
    lines = [f"{title}{suffix}：{result['count']} 人", "数据源：飞书人事当前花名册"]
    lines.extend(
        f"- {row['name']}｜{row['department']}｜{row['job']}｜{row['status']}"
        for row in result["rows"]
    )
    if not result["rows"]:
        lines.append("未找到匹配人员。")
    return "\n".join(lines)


def _date_range(start: str = "", end: str = "") -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    if not start:
        return today - dt.timedelta(days=6), today
    try:
        start_date = dt.date.fromisoformat(start)
        end_date = dt.date.fromisoformat(end or start)
    except ValueError as exc:
        raise HRReadonlyError("invalid_date_format") from exc
    if start_date > end_date:
        raise HRReadonlyError("invalid_date_order")
    if (end_date - start_date).days > 30:
        raise HRReadonlyError("date_range_over_31_days")
    if end_date > today:
        raise HRReadonlyError("future_date_not_allowed")
    return start_date, end_date


def _pick_employee(employees: list[dict], name: str) -> dict:
    normalized = _normalize(name).casefold()
    exact = [item for item in employees if _normalize(
        (item.get("system_fields") or {}).get("name") or "").casefold() == normalized]
    matches = exact or [item for item in employees if normalized in _normalize(
        (item.get("system_fields") or {}).get("name") or "").casefold()]
    if not matches:
        raise HRReadonlyError("employee_not_found")
    if len(matches) != 1:
        raise HRReadonlyError("employee_name_ambiguous")
    return matches[0]


def query_attendance(name: str, start: str = "", end: str = "") -> dict:
    start_date, end_date = _date_range(start, end)
    token = feishu_token()
    employee = _pick_employee(list_employees(token, user_id_type="user_id"), name)
    employee_id = employee.get("user_id")
    if not employee_id:
        raise HRReadonlyError("employee_id_missing")
    result = _request_json(
        "POST",
        f"{FEISHU}/attendance/v1/user_tasks/query?employee_type=employee_id&ignore_invalid_users=true",
        token,
        {"user_ids": [employee_id], "check_date_from": int(start_date.strftime("%Y%m%d")),
         "check_date_to": int(end_date.strftime("%Y%m%d"))},
    )
    data = result.get("data") or {}
    if data.get("unauthorized_user_ids"):
        raise HRReadonlyError("attendance_user_unauthorized")
    if data.get("invalid_user_ids"):
        raise HRReadonlyError("attendance_user_invalid")
    summary = {"scheduled_days": 0, "normal_days": 0, "late_days": 0,
               "early_days": 0, "missing_days": 0, "pending_days": 0,
               "no_check_days": 0}
    for task in data.get("user_task_results") or []:
        records = task.get("records") or []
        in_results = [record.get("check_in_result") for record in records]
        out_results = [record.get("check_out_result") for record in records]
        all_results = [value for value in in_results + out_results if value]
        if all_results and all(value == "NoNeedCheck" for value in all_results):
            summary["no_check_days"] += 1
            continue
        summary["scheduled_days"] += 1
        late = any(value in {"Late", "SeriousLate"} for value in in_results)
        early = "Early" in out_results
        missing = "Lack" in all_results
        pending = "Todo" in all_results
        summary["late_days"] += int(late)
        summary["early_days"] += int(early)
        summary["missing_days"] += int(missing)
        summary["pending_days"] += int(pending)
        if (not (late or early or missing or pending)
                and any(value in {"Normal", "SystemCheck"} for value in all_results)):
            summary["normal_days"] += 1
    fields = employee.get("system_fields") or {}
    return {"ok": True, "source": "feishu_attendance_live",
            "name": fields.get("name") or name, "start": start_date.isoformat(),
            "end": end_date.isoformat(), **summary}


def format_attendance(result: dict) -> str:
    return "\n".join([
        f"考勤只读查询：{result['name']}",
        f"日期：{result['start']} 至 {result['end']}",
        f"应打卡 {result['scheduled_days']} 天；正常 {result['normal_days']} 天；无需打卡 {result['no_check_days']} 天",
        f"异常汇总：迟到 {result['late_days']} 天，早退 {result['early_days']} 天，缺卡 {result['missing_days']} 天，未完成 {result['pending_days']} 天",
        "数据源：飞书考勤实时结果（只读，未写入任何表格）",
    ])


def format_error(exc: Exception) -> str:
    return ERROR_MESSAGES.get(str(exc), "查询暂时失败，请稍后重试或联系管理员。")


def execute(command: dict) -> str:
    kind = command["kind"]
    if kind == "invalid_attendance":
        return "格式：#考勤查询 姓名 [开始日期] [结束日期]\n日期格式为 YYYY-MM-DD，最多查询连续31天。"
    if kind in {"roster", "job"}:
        keyword = command.get("keyword", "")
        result = query_roster(keyword, by_job=kind == "job")
        return format_roster(result, by_job=kind == "job", keyword=keyword)
    result = query_attendance(command["name"], command.get("start", ""), command.get("end", ""))
    return format_attendance(result)
