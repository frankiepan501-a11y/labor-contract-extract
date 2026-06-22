"""劳动合同 OCR 抽取服务 — 扫描飞书劳动合同台账，对新上传的合同附件做 Qwen-VL OCR 提取并回填(待核对)。
全云端，不依赖本地桥接。凭据全部走环境变量。
"""
import os, io, json, base64, datetime, zoneinfo, urllib.request, urllib.parse

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DASHSCOPE_KEY     = os.environ.get("DASHSCOPE_KEY", "")
DASHSCOPE_BASE    = os.environ.get("DASHSCOPE_BASE", "https://dashscope.aliyuncs.com")
BASE_APP_TOKEN    = os.environ.get("CONTRACT_APP_TOKEN", "XDhxbyWQKazDw5s3OJoc7j7cnNh")
TABLE_ID          = os.environ.get("CONTRACT_TABLE_ID", "tbliuozjqvEKT73F")
MAX_PAGES         = int(os.environ.get("OCR_MAX_PAGES", "7"))

TZ = zoneinfo.ZoneInfo("Asia/Shanghai")
FEISHU = "https://open.feishu.cn/open-apis"

# 提取规则驱动的 3 个附件槽
SLOT_PROBATION = "试用期劳动合同附件"
SLOT_REGULAR   = "转正劳动合同附件"
SLOT_RENEWAL   = "续签协议附件"
EXTRACT_SLOTS  = [SLOT_PROBATION, SLOT_REGULAR, SLOT_RENEWAL]

# ---------- HTTP ----------
def _req(method, url, token=None, body=None, raw=False):
    h = {}
    if token: h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        h["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    resp = urllib.request.urlopen(r, timeout=180)
    return resp.read() if raw else json.load(resp)

def feishu_token():
    d = _req("POST", f"{FEISHU}/auth/v3/tenant_access_token/internal", body={
        "app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
    return d["tenant_access_token"]

def list_rows(token):
    url = f"{FEISHU}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{TABLE_ID}/records/search?page_size=500"
    out, pt = [], None
    while True:
        u = url + (f"&page_token={pt}" if pt else "")
        d = _req("POST", u, token, body={})["data"]
        out += d.get("items", [])
        if not d.get("has_more"): break
        pt = d.get("page_token")
    return out

def download_media(token, file_token):
    # bitable 附件下载
    url = f"{FEISHU}/drive/v1/medias/{file_token}/download"
    return _req("GET", url, token, raw=True)

def update_row(token, record_id, fields):
    url = f"{FEISHU}/bitable/v1/apps/{BASE_APP_TOKEN}/tables/{TABLE_ID}/records/{record_id}"
    return _req("PUT", url, token, body={"fields": fields})

# ---------- 渲染 + Qwen-VL ----------
def render_images(pdf_bytes, max_pages=MAX_PAGES, dpi=120):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    imgs = []
    for i in range(min(doc.page_count, max_pages)):
        pix = doc[i].get_pixmap(dpi=dpi)
        imgs.append("data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode())
    return imgs

PROMPTS = {
 SLOT_PROBATION: ('这是【试用期/初次】中文劳动合同扫描件分页图。严格只输出JSON:\n'
   '{"签约公司":"","职称岗位":"","合同期限类型":"固定期限/无固定期限/以完成工作为期限",'
   '"合同开始日":"YYYY-MM-DD","合同到期日":"YYYY-MM-DD","合同期限":"如1年/2年",'
   '"试用期到期":"YYYY-MM-DD","基本工资":"数字或空"}\n找不到填空字符串,日期用横杠,金额只填数字。'),
 SLOT_REGULAR: ('这是【转正】中文劳动合同扫描件分页图。严格只输出JSON:\n'
   '{"签约公司":"","职称岗位":"","合同期限类型":"固定期限/无固定期限/以完成工作为期限",'
   '"合同开始日":"YYYY-MM-DD","合同到期日":"YYYY-MM-DD","合同期限":"如1年/2年",'
   '"基本工资":"数字或空","KPI绩效基数":"数字或空"}\n找不到填空字符串,日期用横杠,金额只填数字。'),
 SLOT_RENEWAL: ('这是【续签劳动合同协议书】中文扫描件分页图。严格只输出JSON:\n'
   '{"签约公司":"","协议开始日":"YYYY-MM-DD","协议到期日":"YYYY-MM-DD","协议期限":"如1年/2年"}\n'
   '找不到填空字符串,日期用横杠。'),
}

def qwen_extract(images, slot):
    content = [{"image": u} for u in images] + [{"text": PROMPTS[slot]}]
    body = {"model": "qwen-vl-max", "input": {"messages": [{"role": "user", "content": content}]}}
    r = _req("POST", f"{DASHSCOPE_BASE}/api/v1/services/aigc/multimodal-generation/generation",
             token=DASHSCOPE_KEY, body=body)
    out = r["output"]["choices"][0]["message"]["content"]
    txt = out[0]["text"] if isinstance(out, list) else out
    txt = txt.strip().strip("`")
    if txt.startswith("json"): txt = txt[4:]
    s, e = txt.find("{"), txt.rfind("}")
    return json.loads(txt[s:e+1])

# ---------- 映射 ----------
def _ms(d):
    if not d: return None
    try:
        y, m, dd = [int(x) for x in d.replace("/", "-").split("-")[:3]]
        return int(datetime.datetime(y, m, dd, tzinfo=TZ).timestamp() * 1000)
    except Exception:
        return None

def _num(v):
    if v in (None, ""): return None
    s = "".join(c for c in str(v) if c.isdigit() or c == ".")
    return float(s) if s else None

def _subject(name):
    if not name: return None
    for k in ("奥得尔", "锦米", "堃铎"):
        if k in name: return k
    return "其他"

def map_fields(slot, data):
    f = {"AI核对状态": "待核对"}
    note = ""
    if slot == SLOT_PROBATION:
        f["员工状态"] = "试用期"
        if data.get("签约公司"): f["合同主体(签订公司)"] = _subject(data["签约公司"])
        if data.get("职称岗位"): f["职称(劳动合同原文)"] = data["职称岗位"]
        if data.get("合同期限类型") in ("固定期限", "无固定期限"): f["合同类型"] = data["合同期限类型"]
        if _ms(data.get("合同开始日")): f["合同开始日期"] = _ms(data["合同开始日"])
        if _ms(data.get("合同到期日")): f["合同到期日期"] = _ms(data["合同到期日"])
        if data.get("合同期限"): f["合同期限"] = data["合同期限"]
        if _ms(data.get("试用期到期")): f["试用期到期日"] = _ms(data["试用期到期"])
        if _num(data.get("基本工资")) is not None: f["固定底薪(合同)"] = _num(data["基本工资"])
        note = f"【AI已解析·待核对】试用期劳动合同: {data}"
    elif slot == SLOT_REGULAR:
        f["员工状态"] = "转正"
        if data.get("签约公司"): f["合同主体(签订公司)"] = _subject(data["签约公司"])
        if data.get("职称岗位"): f["职称(劳动合同原文)"] = data["职称岗位"]
        if data.get("合同期限类型") in ("固定期限", "无固定期限"): f["合同类型"] = data["合同期限类型"]
        if _ms(data.get("合同开始日")): f["合同开始日期"] = _ms(data["合同开始日"])
        if _ms(data.get("合同到期日")): f["合同到期日期"] = _ms(data["合同到期日"])
        if data.get("合同期限"): f["合同期限"] = data["合同期限"]
        if _num(data.get("基本工资")) is not None: f["固定底薪(合同)"] = _num(data["基本工资"])
        if _num(data.get("KPI绩效基数")) is not None: f["KPI底薪基数(转正合同)"] = _num(data["KPI绩效基数"])
        note = f"【AI已解析·待核对】转正劳动合同(覆盖): {data}"
    elif slot == SLOT_RENEWAL:
        if data.get("签约公司"): f["续签协议签订公司"] = data["签约公司"]
        if _ms(data.get("协议开始日")): f["续签协议开始日期"] = _ms(data["协议开始日"])
        if _ms(data.get("协议到期日")): f["续签协议到期日期"] = _ms(data["协议到期日"])
        if data.get("协议期限"): f["续签协议期限"] = data["协议期限"]
        f["续签状态"] = "已续签完成"
        note = f"【AI已解析·待核对】续签协议: {data}"
    return f, note

# ---------- 扫描 ----------
def _att_list(row_fields, slot):
    v = row_fields.get(slot)
    return v if isinstance(v, list) else []

def scan(dry_run=True, limit=None):
    token = feishu_token()
    rows = list_rows(token)
    report = []
    n = 0
    for r in rows:
        rf = r["fields"]
        name = rf.get("员工姓名")
        name = name[0]["text"] if isinstance(name, list) and name else name
        done = set()
        rec = rf.get("_解析记录(系统)")
        rec = rec[0]["text"] if isinstance(rec, list) and rec else (rec or "")
        try: done = set(json.loads(rec)) if rec.strip().startswith("[") else set()
        except Exception: done = set()
        merged = {}
        notes = []
        new_tokens = []
        for slot in EXTRACT_SLOTS:
            for att in _att_list(rf, slot):
                ft = att.get("file_token")
                if not ft or ft in done: continue
                try:
                    pdf = download_media(token, ft)
                    imgs = render_images(pdf)
                    data = qwen_extract(imgs, slot)
                    f, note = map_fields(slot, data)
                    merged.update(f)
                    notes.append(note)
                    new_tokens.append(ft)
                except Exception as ex:
                    notes.append(f"[{slot} 解析失败:{ex}]")
        if not new_tokens:
            if notes:  # 全部失败也写错误备注便于排查,但不记入解析记录(下轮重试)
                report.append({"员工": name, "errors": notes})
                if not dry_run:
                    update_row(token, r["record_id"], {"备注": (" | ".join(notes))[:500]})
            continue
        if notes:
            merged["备注"] = (" | ".join(notes))[:2000]
        merged["_解析记录(系统)"] = json.dumps(list(done | set(new_tokens)), ensure_ascii=False)
        report.append({"员工": name, "record_id": r["record_id"], "新解析附件数": len(new_tokens), "回填字段": merged})
        n += 1
        if not dry_run:
            update_row(token, r["record_id"], merged)
        if limit and n >= limit:
            break
    return {"dry_run": dry_run, "处理行数": n, "明细": report}

# ---------- 到期提醒(纯云端,无OCR) ----------
HR_CHAT_ID  = os.environ.get("HR_CHAT_ID", "oc_3bb2d20ed8c37cef6e0d05e027c42854")
FRANKIE_OID = os.environ.get("FRANKIE_OPEN_ID", "ou_629ce01f4bc31de078e10fcb038dbf78")
WU_OID      = os.environ.get("WU_OPEN_ID", "ou_c65fc5c31c650790db623640b7ac74f7")  # 吴晓丹 COO

def _txt(v):
    if v is None: return ""
    if isinstance(v, list): return "".join((x.get("text", "") if isinstance(x, dict) else str(x)) for x in v)
    if isinstance(v, dict): return v.get("text", "")
    return str(v)

def _days_left(due_ms):
    today = datetime.datetime.now(TZ).date()
    due = datetime.datetime.fromtimestamp(int(due_ms) / 1000, TZ).date()
    return (due - today).days

def send_msg(token, receive_id, id_type, text):
    url = f"{FEISHU}/im/v1/messages?receive_id_type={id_type}"
    return _req("POST", url, token, body={"receive_id": receive_id, "msg_type": "text",
                                          "content": json.dumps({"text": text}, ensure_ascii=False)})

def remind(dry_run=True):
    token = feishu_token()
    rows = list_rows(token)
    items = []
    for r in rows:
        f = r["fields"]
        if _txt(f.get("员工状态")) == "离职" or _txt(f.get("合同类型")) == "无固定期限":
            continue
        due = f.get("续签协议到期日期") or f.get("合同到期日期")
        if not due:
            continue
        d = _days_left(due)
        if d > 60:
            continue
        items.append({"name": _txt(f.get("员工姓名")), "job": _txt(f.get("职务(飞书人事·真相源)")),
                      "due": int(due), "days": d, "coop": _txt(f.get("续签状态"))})
    items.sort(key=lambda x: x["days"])
    if not items:
        return {"dry_run": dry_run, "count": 0, "note": "无60天内到期合同,不发提醒"}
    p0 = any(x["days"] <= 7 for x in items)
    emoji, lvl = ("🔴", "P0") if p0 else ("🟠", "P1")
    lines = [f"{emoji} [HR·{lvl}] 劳动合同到期提醒 · {len(items)}份待处理", ""]
    for x in items:
        due_s = datetime.datetime.fromtimestamp(x["due"] / 1000, TZ).strftime("%Y/%m/%d")
        if x["days"] < 0:   flag = f"⚠️已超期{-x['days']}天"
        elif x["days"] <= 7:  flag = f"🔴剩{x['days']}天"
        elif x["days"] <= 30: flag = f"🟠剩{x['days']}天"
        else:                 flag = f"剩{x['days']}天"
        lines.append(f"• {x['name']}（{x['job']}）到期{due_s} · {flag} · 续签状态:{x['coop'] or '待提醒'}")
    lines += ["", "👉 处理: 在劳动合同台账更新续签状态/上传续签协议"]
    body = "\n".join(lines)
    if not dry_run:
        send_msg(token, HR_CHAT_ID, "chat_id", body)        # 人事及行政管理群(=人事部)
        for oid in (WU_OID, FRANKIE_OID):                    # 吴晓丹 + 潘总, 每次都发
            try: send_msg(token, oid, "open_id", body)
            except Exception: pass
    return {"dry_run": dry_run, "count": len(items), "p0": p0, "preview": body}

# ---------- 文件名日期解析(纯云端,无OCR) ----------
import re
_DATE_RANGE = re.compile(r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})\D{0,3}(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})")

def parse_filenames(dry_run=True):
    token = feishu_token()
    rows = list_rows(token)
    out = []
    for r in rows:
        f = r["fields"]
        if f.get("合同到期日期"):
            continue  # 已有到期日,不覆盖
        names = []
        for slot in ("转正劳动合同附件", "试用期劳动合同附件"):
            for a in (f.get(slot) or []):
                if a.get("name"): names.append(a["name"])
        hit = None
        for nm in names:
            m = _DATE_RANGE.search(nm)
            if m:
                y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
                try:
                    s = int(datetime.datetime(y1, m1, d1, tzinfo=TZ).timestamp() * 1000)
                    e = int(datetime.datetime(y2, m2, d2, tzinfo=TZ).timestamp() * 1000)
                    hit = (s, e, nm); break
                except Exception:
                    pass
        if not hit:
            continue
        s, e, nm = hit
        fields = {"合同开始日期": s, "合同到期日期": e,
                  "备注": f"【文件名解析】起止日取自附件名「{nm}」,请人事核对"}
        out.append({"员工": _txt(f.get("员工姓名")), "fields": fields})
        if not dry_run:
            update_row(token, r["record_id"], fields)
    return {"dry_run": dry_run, "filled": len(out), "detail": out}

# ---------- FastAPI ----------
import threading
_LOCK = threading.Lock()
_LAST = {"ts": "", "result": None}

def _bg_scan(limit):
    if not _LOCK.acquire(blocking=False):
        return
    try:
        r = scan(dry_run=False, limit=limit)
        _LAST["result"] = {"处理行数": r["处理行数"], "明细": r.get("明细")}
    except Exception as ex:
        _LAST["result"] = {"error": str(ex)}
    finally:
        _LOCK.release()

try:
    from fastapi import FastAPI
    api = FastAPI(title="labor-contract-extract")

    @api.get("/health")
    def health(): return {"ok": True, "v": 5, "last": _LAST}

    @api.post("/scan")
    def scan_ep(dry_run: bool = False, limit: int = 0, bg: bool = False):
        # (OCR, 当前未用) bg=true 后台线程
        if bg and not dry_run:
            threading.Thread(target=_bg_scan, args=(limit or None,), daemon=True).start()
            return {"started": True}
        return scan(dry_run=dry_run, limit=limit or None)

    @api.post("/remind")
    def remind_ep(dry_run: bool = False):
        return remind(dry_run=dry_run)

    @api.post("/parse-filenames")
    def parse_ep(dry_run: bool = False):
        return parse_filenames(dry_run=dry_run)
except Exception:
    api = None

if __name__ == "__main__":
    import sys
    dry = "--commit" not in sys.argv
    print(json.dumps(scan(dry_run=dry, limit=5), ensure_ascii=False, indent=2))
