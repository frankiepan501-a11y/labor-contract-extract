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
    def health(): return {"ok": True, "v": 3, "last": _LAST}

    @api.post("/scan")
    def scan_ep(dry_run: bool = False, limit: int = 0, bg: bool = False):
        # bg=true: 后台线程跑(避开网关超时)，立即返回；cron 用这个
        if bg and not dry_run:
            threading.Thread(target=_bg_scan, args=(limit or None,), daemon=True).start()
            return {"started": True}
        return scan(dry_run=dry_run, limit=limit or None)
except Exception:
    api = None

if __name__ == "__main__":
    import sys
    dry = "--commit" not in sys.argv
    print(json.dumps(scan(dry_run=dry, limit=5), ensure_ascii=False, indent=2))
