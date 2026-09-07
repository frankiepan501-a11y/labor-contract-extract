"""Protected minimum-data HR interfaces for downstream workflows.

The module owns all EHR and attendance reads. Callers receive only the fields
needed for their business decision; raw API payloads and cross-App open_ids are
never returned.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hmac
import os

import hr_readonly


def authorized(authorization: str) -> bool:
    expected = os.environ.get("HR_INTERNAL_API_TOKEN", "")
    prefix = "Bearer "
    if not expected or not authorization.startswith(prefix):
        return False
    return hmac.compare_digest(authorization[len(prefix):], expected)


def _system_fields(employee: dict) -> dict:
    return employee.get("system_fields") or {}


def people_minimal(purpose: str, names: list[str] | None = None) -> dict:
    if purpose not in {"amazon_kpi", "warehouse_owner_audit"}:
        raise hr_readonly.HRReadonlyError("invalid_people_purpose")
    wanted_names = {str(name).strip() for name in (names or []) if str(name).strip()}
    if purpose == "warehouse_owner_audit" and not wanted_names:
        raise hr_readonly.HRReadonlyError("people_names_required")
    token = hr_readonly.feishu_token()
    employees = hr_readonly.list_employees(token, statuses=(2, 4, 5))
    rows = []
    for employee in employees:
        fields = _system_fields(employee)
        name = fields.get("name") or ""
        job_name = (fields.get("job") or {}).get("name") or ""
        if purpose == "amazon_kpi" and job_name != "亚马逊运营专员":
            continue
        if wanted_names and name not in wanted_names:
            continue
        status_code = fields.get("status")
        row = {"name": name, "active": status_code in (2, 4)}
        if purpose == "amazon_kpi":
            row.update({
                "job": job_name or "未填写职务",
                "conversion_date": fields.get("conversion_date") or "",
                "hire_date": fields.get("hire_date") or "",
            })
        rows.append(row)
    rows.sort(key=lambda row: row["name"])
    return {"ok": True, "source": "hr_assistant_ehr_live", "count": len(rows), "rows": rows}


def _month_range(month: str) -> tuple[int, int, str]:
    try:
        first = dt.date.fromisoformat(month + "-01")
    except ValueError as exc:
        raise hr_readonly.HRReadonlyError("invalid_month") from exc
    last_day = calendar.monthrange(first.year, first.month)[1]
    return int(first.strftime("%Y%m%d")), int(first.replace(day=last_day).strftime("%Y%m%d")), first.strftime("%Y/%m")


def _attendance_metrics(token: str, employee_id: str, start_date: int, end_date: int) -> dict:
    tasks = hr_readonly._request_json(
        "POST",
        f"{hr_readonly.FEISHU}/attendance/v1/user_tasks/query?employee_type=employee_id",
        token,
        {"user_ids": [employee_id], "check_date_from": start_date, "check_date_to": end_date},
    )
    approvals = hr_readonly._request_json(
        "POST",
        f"{hr_readonly.FEISHU}/attendance/v1/user_approvals/query?employee_type=employee_id",
        token,
        {"user_ids": [employee_id], "check_date_from": start_date, "check_date_to": end_date},
    )
    for response in (tasks, approvals):
        data = response.get("data") or {}
        if data.get("unauthorized_user_ids"):
            raise hr_readonly.HRReadonlyError("attendance_user_unauthorized")
        if data.get("invalid_user_ids"):
            raise hr_readonly.HRReadonlyError("attendance_user_invalid")
    late_days = late_seconds = holiday_days = 0
    sick_days = annual_days = personal_hours = 0.0
    wedding_days = bereavement_days = maternity_days = comp_leave_hours = 0.0
    normal_days = work_days_total = 0
    for task in (tasks.get("data") or {}).get("user_task_results") or []:
        day = str(task.get("day") or 0)
        if len(day) != 8:
            continue
        weekday = dt.date(int(day[:4]), int(day[4:6]), int(day[6:8])).weekday()
        records = task.get("records") or []
        check_in = [record.get("check_in_result") or "" for record in records]
        check_out = [record.get("check_out_result") or "" for record in records]
        # Preserve the legacy workflow rule: an empty result is treated the same
        # as an all-NoNeedCheck day; weekdays count as statutory holidays.
        if all(value == "NoNeedCheck" for value in check_in + check_out):
            if weekday < 5:
                holiday_days += 1
            continue
        work_days_total += 1
        if "Normal" in check_in or "Late" in check_in:
            normal_days += 1
        if "Late" in check_in:
            late_days += 1
            for record in records:
                if record.get("check_in_result") != "Late":
                    continue
                shift = int(record.get("check_in_shift_time") or 0)
                actual = int(record.get("check_in_record_time") or 0)
                if not actual:
                    actual = int((record.get("check_in_record") or {}).get("check_time") or 0)
                if actual > shift > 0:
                    late_seconds += actual - shift
    for approval in (approvals.get("data") or {}).get("user_approvals") or []:
        for leave in approval.get("leaves") or []:
            name = (leave.get("i18n_names") or {}).get("ch") or ""
            hours = float(leave.get("interval") or 0) / 3600
            if "病假" in name:
                sick_days += hours / 7.5
            elif "年假" in name:
                annual_days += hours / 7.5
            elif "事假" in name:
                personal_hours += hours
            elif "婚假" in name:
                wedding_days += hours / 7.5
            elif "丧假" in name:
                bereavement_days += hours / 7.5
            elif "产检" in name or "产假" in name:
                maternity_days += hours / 7.5
            elif "调休" in name or "补休" in name:
                comp_leave_hours += hours
    late_minutes = round(late_seconds / 60)
    sick_days = round(sick_days, 2)
    annual_days = round(annual_days, 2)
    personal_hours = round(personal_hours, 2)
    return {
        "full_attendance_days": work_days_total + holiday_days,
        "actual_attendance_days": normal_days + holiday_days,
        "late_minutes": late_minutes,
        "late_days": late_days,
        "sick_days": sick_days,
        "annual_days": annual_days,
        "personal_hours": personal_hours,
        "personal_days": round(personal_hours / 7.5, 2),
        "statutory_holiday_days": holiday_days,
        "wedding_days": round(wedding_days, 2),
        "bereavement_days": round(bereavement_days, 2),
        "maternity_days": round(maternity_days, 2),
        "comp_leave_hours": round(comp_leave_hours, 2),
        "is_full_attendance": "是" if late_days <= 2 and late_minutes <= 30 and sick_days == 0 and personal_hours == 0 else "否",
    }


def monthly_attendance(month: str, previous_names: list[str] | None = None) -> dict:
    start_date, end_date, month_label = _month_range(month)
    token = hr_readonly.feishu_token()
    employees = hr_readonly.list_employees(token, user_id_type="user_id", statuses=(2, 4, 5))
    previous = {str(name).strip() for name in (previous_names or []) if str(name).strip()}
    selected = []
    current_names = []
    departed_names = []
    for employee in employees:
        fields = _system_fields(employee)
        name = fields.get("name") or ""
        status = fields.get("status")
        if status in (2, 4):
            selected.append(employee)
            current_names.append(name)
        elif status == 5 and name in previous:
            selected.append(employee)
            departed_names.append(name)
    rows = []
    for employee in selected:
        fields = _system_fields(employee)
        employee_id = employee.get("user_id")
        if not employee_id:
            raise hr_readonly.HRReadonlyError("employee_id_missing")
        rows.append({
            "name": fields.get("name") or "未命名",
            "job": (fields.get("job") or {}).get("name") or "",
            "departed": fields.get("status") == 5,
            **_attendance_metrics(token, employee_id, start_date, end_date),
        })
    return {
        "ok": True,
        "source": "hr_assistant_attendance_live",
        "month": month_label,
        "current_names": sorted(current_names),
        "departed_names": sorted(departed_names),
        "rows": rows,
    }
