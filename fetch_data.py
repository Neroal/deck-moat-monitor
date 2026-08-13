#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DECK 護城河監控 — SEC EDGAR 數據抓取腳本
================================================
資料來源：SEC EDGAR XBRL API（官方、免費、無需 API Key）
  哨兵 API  : https://data.sec.gov/submissions/CIK0000910521.json
  數據 API  : https://data.sec.gov/api/xbrl/companyfacts/CIK0000910521.json

執行邏輯：
  1. 先打「哨兵 API」取得最新 10-Q / 10-K 的申報日（filingDate）。
  2. 與現有 data.json 的 meta.latest_filing.filed 比對：
     - 有新申報          → 完整抓取 companyfacts、重算所有季度、寫入 data.json
     - 無新申報          → 若距離上次檢查 >= 7 天，僅更新 last_checked（每週心跳 commit）
     - 其餘情況          → 不寫任何檔案、不產生 commit
  3. 在 GitHub Actions 環境下，將 commit 訊息寫入 $GITHUB_OUTPUT 供 workflow 使用。

已處理的 EDGAR 常見陷阱：
  - Q4 無獨立申報       → 以「全年 − (Q1+Q2+Q3)」反推
  - 10-Q 內含 6/9 個月累計期間 → 以期間長度 75~105 天過濾出單季
  - 同一期間被多次申報（比較期重述）→ 以 filed 日期最新者為準
  - 2024-09-17 六股拆一股票分割 → 分割前申報的股數 ×6、EPS ÷6，全部換算為分割後口徑
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone

# ─────────────────────────────────────────────────────────────
# 基本設定
# ─────────────────────────────────────────────────────────────
CIK = "0000910521"                     # Deckers Outdoor Corporation
TICKER = "DECK"
COMPANY = "Deckers Outdoor Corporation"

# SEC 要求 User-Agent 必須包含可聯絡的身份資訊
USER_AGENT = "DECK Moat Monitor a0987385316@gmail.com"

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
HEARTBEAT_DAYS = 7                     # 每週心跳：無新財報時，最多 7 天更新一次 last_checked

# 股票分割紀錄：申報日早於 split date 的股數/每股數據需換算
SPLITS = [
    {"date": "2024-09-17", "ratio": 6.0},   # 2024/9/17 六股拆一
]

# XBRL 標籤候選清單（公司偶爾換 tag 申報，依序 fallback）
TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "cogs": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    ],
    "net_income": ["NetIncomeLoss"],
    "eps": ["EarningsPerShareDiluted"],
    # 資產負債表（時點數）
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "debt_lt_cur": ["LongTermDebtCurrent"],
    "debt_st": ["ShortTermBorrowings", "OtherShortTermBorrowings"],
    "lease_cur": ["OperatingLeaseLiabilityCurrent"],
    "lease_noncur": ["OperatingLeaseLiabilityNoncurrent"],
}


# ─────────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────────
def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  請求失敗（{e}），{2 ** attempt} 秒後重試…")
            time.sleep(2 ** attempt)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def fiscal_label(end: date):
    """Deckers 財年 3 月底結帳：2025-06-30 → FY2026 Q1。回傳 (fy, q, label)。"""
    m = end.month
    if 4 <= m <= 6:
        fy, q = end.year + 1, 1
    elif 7 <= m <= 9:
        fy, q = end.year + 1, 2
    elif 10 <= m <= 12:
        fy, q = end.year + 1, 3
    else:  # 1~3 月
        fy, q = end.year, 4
    return fy, q, f"FY{fy} Q{q}"


def split_factor(filed: str) -> float:
    """申報日早於分割日者，回傳累計分割比率（股數乘、EPS 除）。"""
    factor = 1.0
    for s in SPLITS:
        if filed < s["date"]:
            factor *= s["ratio"]
    return factor


def set_github_output(msg: str):
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"commit_message={msg}\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────
# XBRL 解析
# ─────────────────────────────────────────────────────────────
def collect_facts(facts: dict, tag_candidates: list, unit_keys: tuple):
    """
    從 companyfacts 中收集指定概念的所有事實。
    合併所有候選 tag，去重規則：同一 (start, end) 期間以 filed 最新者為準。
    回傳 list[{start?, end, val, filed}]
    """
    merged = {}
    for taxonomy in ("us-gaap", "dei"):
        tax = facts.get(taxonomy, {})
        for tag in tag_candidates:
            node = tax.get(tag)
            if not node:
                continue
            units = node.get("units", {})
            for uk in unit_keys:
                for item in units.get(uk, []):
                    if item.get("val") is None or not item.get("end"):
                        continue
                    key = (item.get("start"), item["end"])
                    prev = merged.get(key)
                    if prev is None or item.get("filed", "") > prev.get("filed", ""):
                        merged[key] = {
                            "start": item.get("start"),
                            "end": item["end"],
                            "val": item["val"],
                            "filed": item.get("filed", ""),
                        }
    return list(merged.values())


def quarterly_flows(items: list):
    """
    流量科目（營收、成本、淨利、EPS）：
    - 期間 75~105 天 → 單季
    - 期間 350~380 天 → 全年（供 Q4 反推）
    回傳 (quarters: {(fy,q): fact}, annuals: {fy: fact})
    """
    quarters, annuals = {}, {}
    for it in items:
        if not it["start"]:
            continue
        start, end = parse_date(it["start"]), parse_date(it["end"])
        days = (end - start).days
        if 75 <= days <= 105:
            fy, q, _ = fiscal_label(end)
            key = (fy, q)
            if key not in quarters or it["filed"] > quarters[key]["filed"]:
                quarters[key] = it
        elif 350 <= days <= 380:
            fy, _, _ = fiscal_label(end)
            if fy not in annuals or it["filed"] > annuals[fy]["filed"]:
                annuals[fy] = it
    return quarters, annuals


def derive_q4(quarters: dict, annuals: dict):
    """Q4 = 全年 − (Q1+Q2+Q3)。三季齊備且尚無 Q4 時才反推。"""
    for fy, ann in annuals.items():
        if (fy, 4) in quarters:
            continue
        q123 = [quarters.get((fy, q)) for q in (1, 2, 3)]
        if all(q123):
            q4_val = ann["val"] - sum(x["val"] for x in q123)
            quarters[(fy, 4)] = {
                "start": None, "end": ann["end"],
                "val": q4_val, "filed": ann["filed"],
                "derived": True,
            }
    return quarters


def instant_by_quarter(items: list):
    """時點科目（現金、負債等）：instant 事實直接對應到該財季季末。"""
    out = {}
    for it in items:
        if it["start"]:                      # instant 事實沒有 start
            continue
        end = parse_date(it["end"])
        # 只收季末附近的時點數（月底 ±10 天內，避免零星日期）
        if end.month not in (3, 6, 9, 12):
            continue
        fy, q, _ = fiscal_label(end)
        key = (fy, q)
        if key not in out or it["filed"] > out[key]["filed"]:
            out[key] = it
    return out


def shares_by_quarter(facts: dict):
    """
    流通股數：dei:EntityCommonStockSharesOutstanding（每次申報封面的即時股數）。
    封面日通常落在季末後 3~6 週，故映射到「最近一個已結束的財季」。
    申報日早於股票分割者 ×ratio 換算為分割後口徑。
    """
    items = collect_facts(facts, ["EntityCommonStockSharesOutstanding"], ("shares",))
    out = {}
    for it in items:
        if it["start"]:
            continue
        cover = parse_date(it["end"])
        # 最近一個已結束的財季季末（3/6/9/12 月的月底）
        qe_month = ((cover.month - 1) // 3) * 3  # 0,3,6,9 → 上一季末月
        if qe_month == 0:
            qend = date(cover.year - 1, 12, 31)
        else:
            last_day = {3: 31, 6: 30, 9: 30}[qe_month]
            qend = date(cover.year, qe_month, last_day)
        fy, q, _ = fiscal_label(qend)
        val = it["val"] * split_factor(it["filed"])
        key = (fy, q)
        if key not in out or it["filed"] > out[key]["filed"]:
            out[key] = {"end": qend.isoformat(), "val": val, "filed": it["filed"]}
    return out


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def load_existing():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def latest_financial_filing(submissions: dict):
    """從哨兵 API 找出最新的 10-Q / 10-K 申報。"""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    best = None
    for form, fdate in zip(forms, dates):
        if form in ("10-Q", "10-K"):
            if best is None or fdate > best["filed"]:
                best = {"form": form, "filed": fdate}
    return best


def build_dataset(latest_filing: dict) -> dict:
    print("抓取 companyfacts（完整 XBRL 歷史數據）…")
    facts_root = http_get_json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
    )
    facts = facts_root.get("facts", {})

    # 流量科目
    rev_q, rev_a = quarterly_flows(collect_facts(facts, TAGS["revenue"], ("USD",)))
    cogs_q, cogs_a = quarterly_flows(collect_facts(facts, TAGS["cogs"], ("USD",)))
    ni_q, ni_a = quarterly_flows(collect_facts(facts, TAGS["net_income"], ("USD",)))
    eps_q, eps_a = quarterly_flows(collect_facts(facts, TAGS["eps"], ("USD/shares",)))

    derive_q4(rev_q, rev_a)
    derive_q4(cogs_q, cogs_a)
    derive_q4(ni_q, ni_a)
    derive_q4(eps_q, eps_a)

    # EPS 分割換算（以「該事實的申報日」判斷是否為分割前口徑）
    for key, it in eps_q.items():
        it["val"] = it["val"] / split_factor(it["filed"])

    # 時點科目
    cash_q = instant_by_quarter(collect_facts(facts, TAGS["cash"], ("USD",)))
    sti_q = instant_by_quarter(collect_facts(facts, TAGS["sti"], ("USD",)))
    dlt_q = instant_by_quarter(collect_facts(facts, TAGS["debt_lt"], ("USD",)))
    dltc_q = instant_by_quarter(collect_facts(facts, TAGS["debt_lt_cur"], ("USD",)))
    dst_q = instant_by_quarter(collect_facts(facts, TAGS["debt_st"], ("USD",)))
    lc_q = instant_by_quarter(collect_facts(facts, TAGS["lease_cur"], ("USD",)))
    lnc_q = instant_by_quarter(collect_facts(facts, TAGS["lease_noncur"], ("USD",)))
    sh_q = shares_by_quarter(facts)

    # 以「營收有單季數據」的季度為主軸組裝
    quarters = []
    for (fy, q), rev in sorted(rev_q.items()):
        end = rev["end"]
        _, _, label = fiscal_label(parse_date(end))
        cogs = cogs_q.get((fy, q))
        ni = ni_q.get((fy, q))
        eps = eps_q.get((fy, q))

        def val(d, default=None):
            return d["val"] if d else default

        revenue = rev["val"]
        cogs_v = val(cogs)
        gm = (revenue - cogs_v) / revenue if (cogs_v is not None and revenue) else None
        ni_v = val(ni)
        nm = ni_v / revenue if (ni_v is not None and revenue) else None

        quarters.append({
            "label": label,
            "fy": fy,
            "q": q,
            "end": end,
            "revenue": revenue,
            "cogs": cogs_v,
            "gross_margin": round(gm * 100, 2) if gm is not None else None,
            "net_income": ni_v,
            "net_margin": round(nm * 100, 2) if nm is not None else None,
            "eps": round(val(eps), 4) if eps else None,
            "cash": val(cash_q.get((fy, q)), 0),
            "sti": val(sti_q.get((fy, q)), 0),
            "debt_interest": (
                val(dlt_q.get((fy, q)), 0)
                + val(dltc_q.get((fy, q)), 0)
                + val(dst_q.get((fy, q)), 0)
            ),
            "debt_lease": (
                val(lc_q.get((fy, q)), 0) + val(lnc_q.get((fy, q)), 0)
            ),
            "shares": round(val(sh_q.get((fy, q), None), 0)) or None,
        })

    # 只保留數據完整度足夠的季度（至少要有毛利率）
    quarters = [x for x in quarters if x["gross_margin"] is not None]
    quarters.sort(key=lambda x: x["end"])

    latest = quarters[-1] if quarters else None
    return {
        "meta": {
            "ticker": TICKER,
            "company": COMPANY,
            "cik": CIK,
            "source": "SEC EDGAR XBRL (data.sec.gov)",
            "sample": False,
            "last_checked": now_iso(),
            "last_updated": now_iso(),
            "latest_filing": latest_filing,
            "latest_quarter": latest["label"] if latest else None,
        },
        "quarters": quarters,
    }


def main():
    print(f"=== DECK 護城河監控 · 數據更新檢查 · {now_iso()} ===")
    existing = load_existing()

    # 1. 哨兵：最新申報日
    print("查詢 submissions（哨兵 API）…")
    submissions = http_get_json(
        f"https://data.sec.gov/submissions/CIK{CIK}.json"
    )
    latest_filing = latest_financial_filing(submissions)
    if not latest_filing:
        print("找不到任何 10-Q/10-K 申報，異常結束。")
        sys.exit(1)
    print(f"EDGAR 最新財報申報：{latest_filing['form']}，filed {latest_filing['filed']}")

    prev_filed = ""
    is_sample = True
    if existing:
        meta = existing.get("meta", {})
        prev_filed = (meta.get("latest_filing") or {}).get("filed", "")
        is_sample = bool(meta.get("sample", False))

    # 2. 判斷是否需要完整更新
    if is_sample or not existing or latest_filing["filed"] > prev_filed:
        reason = "首次建置/範例資料替換" if (is_sample or not existing) else "偵測到新財報"
        print(f"→ {reason}，執行完整抓取…")
        time.sleep(0.5)  # 禮貌性間隔（SEC 限制為每秒 10 次，遠低於此）
        data = build_dataset(latest_filing)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        lq = data["meta"]["latest_quarter"]
        n = len(data["quarters"])
        msg = f"data: update to {lq} ({latest_filing['form']} filed {latest_filing['filed']}, {n} quarters)"
        print(f"→ 已寫入 data.json（{n} 季）。commit: {msg}")
        set_github_output(msg)
        return

    # 3. 心跳：無新財報，但距上次檢查已滿 7 天
    last_checked_s = existing["meta"].get("last_checked", "1970-01-01T00:00:00Z")
    try:
        last_checked = datetime.strptime(last_checked_s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        last_checked = datetime(1970, 1, 1, tzinfo=timezone.utc)

    if datetime.now(timezone.utc) - last_checked >= timedelta(days=HEARTBEAT_DAYS):
        existing["meta"]["last_checked"] = now_iso()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=1)
        msg = "chore: heartbeat check (no new filing)"
        print(f"→ 無新財報，寫入每週心跳。commit: {msg}")
        set_github_output(msg)
        return

    # 4. 無事可做
    print("→ 無新財報，距上次心跳未滿 7 天，不產生任何變更。")


if __name__ == "__main__":
    main()
