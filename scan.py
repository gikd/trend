#!/usr/bin/env python3
"""[매일 16:00 KST] 추세추종 워치리스트 스캐너 → docs/data.json.

로직: "가는 게 더 간다 + 좋은 섹터 안의 좋은 종목은 더 간다"
1. 네이버 시총 API로 코스피+코스닥 전종목 명단 (우선주·스팩·리츠·거래정지 제외)
2. 업종 API(79개)로 종목→섹터 매핑
3. 유동성 프리필터 통과 종목의 일봉 280개 수집 (fchart)
4. 모멘텀 지표 계산 → 백분위 랭킹 → 섹터 강도 가점 → 3개 시간축 리스트
5. 직전 data.json과 비교해 신규 진입/이탈 표시
"""
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

DOCS = Path(__file__).parent / "docs"
OUT = DOCS / "data.json"
ARCHIVE = DOCS / "history"

LISTING = "https://m.stock.naver.com/api/stocks/marketValue/{m}?page={p}&pageSize=100"
INDUSTRY_LIST = "https://m.stock.naver.com/api/stocks/industry?page={p}&pageSize=100"
INDUSTRY_MEMBERS = "https://m.stock.naver.com/api/stocks/industry/{no}?page={p}&pageSize=100"
FCHART = "https://fchart.stock.naver.com/sise.nhn?symbol={c}&timeframe=day&count=280&requestType=0"

# 유동성 기준 (원)
MIN_VAL20 = 3_000_000_000      # 리스트 편입: 20일 평균 거래대금 30억+
PREFILTER_VAL = 500            # 히스토리 수집 대상: 당일 거래대금 5억+ (백만원 단위)
PREFILTER_CAP = 2_000          # 또는 시총 2,000억+ (억원 단위)

KST = timezone(timedelta(hours=9))


def fetch_json(url, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                return None
    return None


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def fetch_listing():
    """코스피+코스닥 전종목. 반환: {code: {name, market, cap(억), val(백만)}}"""
    stocks = {}
    for market in ("KOSPI", "KOSDAQ"):
        page = 1
        while True:
            data = fetch_json(LISTING.format(m=market, p=page))
            if not data or not data.get("stocks"):
                break
            for s in data["stocks"]:
                code, name = s.get("itemCode", ""), s.get("stockName", "")
                if s.get("stockEndType") != "stock":
                    continue
                if not code.endswith("0"):       # 우선주 (보통주는 코드 끝 0)
                    continue
                if "스팩" in name or "리츠" in name:
                    continue
                if (s.get("tradeStopType") or {}).get("name") != "TRADING":
                    continue
                stocks[code] = {
                    "name": name, "market": market,
                    "cap": num(s.get("marketValue")),
                    "val": num(s.get("accumulatedTradingValue")),
                }
            if len(data["stocks"]) < 100:
                break
            page += 1
    return stocks


def fetch_sector_map():
    """업종 79개 순회 → {code: 업종명}"""
    groups = []
    page = 1
    while True:
        data = fetch_json(INDUSTRY_LIST.format(p=page))
        if not data or not data.get("groups"):
            break
        groups += data["groups"]
        if len(groups) >= data.get("totalCount", 0):
            break
        page += 1

    sector = {}

    def members(g):
        out, page = [], 1
        while True:
            data = fetch_json(INDUSTRY_MEMBERS.format(no=g["no"], p=page))
            if not data or not data.get("stocks"):
                break
            out += [s.get("itemCode", "") for s in data["stocks"]]
            if len(data["stocks"]) < 100:
                break
            page += 1
        return g["name"], out

    with ThreadPoolExecutor(8) as ex:
        for name, codes in ex.map(members, groups):
            for c in codes:
                sector[c] = name
    return sector


def fetch_candles(code):
    try:
        req = urllib.request.Request(FCHART.format(c=code), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("euc-kr", errors="ignore")
    except Exception:
        return None
    rows = re.findall(r'data="(\d{8})\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)"', raw)
    if len(rows) < 60:
        return None
    return [(d, int(o), int(h), int(l), int(c), int(v)) for d, o, h, l, c, v in rows]


def pct_rank(sorted_vals, v):
    if not sorted_vals or v is None:
        return 0.0
    import bisect
    i = bisect.bisect_left(sorted_vals, v)
    return i / max(len(sorted_vals) - 1, 1)


def ret(closes, n):
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    return closes[-1] / base - 1 if base else None


def compute(code, meta, candles, sector):
    dates = [r[0] for r in candles]
    highs = [r[2] for r in candles]
    lows = [r[3] for r in candles]
    closes = [r[4] for r in candles]
    vols = [r[5] for r in candles]
    vals = [c * v for c, v in zip(closes, vols)]
    close, high, low = closes[-1], highs[-1], lows[-1]

    ma = lambda n: sum(closes[-n:]) / n if len(closes) >= n else None
    ma20, ma60, ma120 = ma(20), ma(60), ma(120)
    val20 = sum(vals[-21:-1]) / 20 if len(vals) >= 21 else None

    hi52 = max(highs[-252:])
    near52 = close / hi52 if hi52 else 0
    new_high = high >= max(highs[-252:-1]) if len(highs) > 1 else False

    days_above = 0
    if ma20:
        for i in range(len(closes) - 1, 19, -1):
            m = sum(closes[i - 19:i + 1]) / 20
            if closes[i] >= m:
                days_above += 1
            else:
                break

    r1, r5, r21, r63, r126 = (ret(closes, n) for n in (1, 5, 21, 63, 126))
    val_surge = vals[-1] / val20 if val20 else 0
    close_strength = (close - low) / (high - low) if high > low else 0.5
    limit_ups = sum(1 for i in range(max(1, len(closes) - 21), len(closes))
                    if closes[i - 1] and closes[i] / closes[i - 1] - 1 >= 0.295)

    return {
        "code": code, "name": meta["name"], "market": meta["market"],
        "sector": sector.get(code, "기타"), "cap": meta["cap"],
        "close": close, "date": dates[-1],
        "r1": r1, "r5": r5, "r21": r21, "r63": r63, "r126": r126,
        "ma20": ma20, "ma60": ma60, "ma120": ma120, "val20": val20,
        "near52": near52, "new_high": new_high, "days_above": days_above,
        "val_surge": val_surge, "close_strength": close_strength,
        "limit_ups": limit_ups,
        "spark": closes[-60:],
    }


def build_reasons(s, sec_rank):
    r = []
    if s["new_high"]:
        r.append("52주 신고가")
    elif s["near52"] >= 0.95:
        r.append(f"신고가 -{(1 - s['near52']) * 100:.0f}% 근접")
    if s["val_surge"] >= 2:
        r.append(f"거래대금 {s['val_surge']:.0f}배 급증")
    if s["days_above"] >= 10:
        r.append(f"20일선 위 {s['days_above']}일째")
    if s["r21"] is not None and s["r21"] >= 0.10:
        r.append(f"1개월 +{s['r21'] * 100:.0f}%")
    if s["r63"] is not None and s["r63"] >= 0.30:
        r.append(f"3개월 +{s['r63'] * 100:.0f}%")
    if sec_rank and sec_rank <= 5:
        r.append(f"주도섹터 {s['sector']} {sec_rank}위")
    if s["close_strength"] >= 0.8 and (s["r1"] or 0) > 0:
        r.append("고가권 마감")
    if s["limit_ups"] >= 2:
        r.append("급등락 주의")
    if not r:
        r.append("이평선 정배열 추세 지속")
    return r[:4]


def main():
    print("1/4 종목 명단...")
    listing = fetch_listing()
    print(f"  {len(listing)}종목")

    print("2/4 섹터 매핑...")
    sector = fetch_sector_map()
    print(f"  {len(sector)}종목 매핑")

    cands = {c: m for c, m in listing.items()
             if m["val"] >= PREFILTER_VAL or m["cap"] >= PREFILTER_CAP}
    print(f"3/4 일봉 수집... ({len(cands)}종목)")
    stocks = []
    with ThreadPoolExecutor(12) as ex:
        futs = {c: ex.submit(fetch_candles, c) for c in cands}
        for c, f in futs.items():
            candles = f.result()
            if candles:
                stocks.append(compute(c, cands[c], candles, sector))
    print(f"  {len(stocks)}종목 계산 완료")

    print("4/4 스코어링...")
    # 데이터 검증: 최신 거래일 기준으로 통일 (수집 시점 차이로 하루 밀린 종목 제거)
    latest = max(s["date"] for s in stocks)
    stocks = [s for s in stocks if s["date"] == latest]

    # 백분위 랭킹 (수집 유니버스 전체 기준)
    import bisect
    ranks = {}
    for k in ("r21", "r63", "r126", "val_surge"):
        vals = sorted(s[k] for s in stocks if s[k] is not None)
        ranks[k] = vals
    P = lambda k, v: pct_rank(ranks[k], v) if v is not None else 0.0

    # 섹터 강도: 구성종목 5종목+ 섹터의 r5/r21 중앙값
    from statistics import median
    by_sec = {}
    for s in stocks:
        by_sec.setdefault(s["sector"], []).append(s)
    sec_stats = []
    for name, ss in by_sec.items():
        if name == "기타" or len(ss) < 5:
            continue
        sec_stats.append({
            "name": name, "n": len(ss),
            "r5": median(x["r5"] for x in ss if x["r5"] is not None),
            "r21": median(x["r21"] for x in ss if x["r21"] is not None),
        })
    sec_stats.sort(key=lambda x: 0.5 * x["r5"] + 0.5 * x["r21"], reverse=True)
    sec_rank = {x["name"]: i + 1 for i, x in enumerate(sec_stats)}
    n_sec = max(len(sec_stats), 1)
    sec_pct = {x["name"]: 1 - (sec_rank[x["name"]] - 1) / n_sec for x in sec_stats}

    near_comp = lambda s: max(0.0, min(1.0, (s["near52"] - 0.85) / 0.15))
    penalty = lambda s: 10 if s["limit_ups"] >= 2 else 0
    liquid = [s for s in stocks if s["val20"] and s["val20"] >= MIN_VAL20]

    def score_today(s):
        if not (s["ma20"] and s["close"] > s["ma20"] and (s["r1"] or 0) > 0):
            return None
        if not (s["val_surge"] >= 1.5 or s["new_high"]):
            return None
        return (35 * P("val_surge", s["val_surge"]) + 25 * near_comp(s)
                + 20 * s["close_strength"] + 20 * sec_pct.get(s["sector"], 0.3)
                - penalty(s))

    def score_week(s):
        if not (s["ma20"] and s["ma60"] and s["close"] > s["ma20"] > s["ma60"]):
            return None
        return (35 * P("r21", s["r21"]) + 20 * near_comp(s)
                + 15 * min(s["days_above"], 40) / 40 + 10 * P("val_surge", s["val_surge"])
                + 20 * sec_pct.get(s["sector"], 0.3) - penalty(s))

    def score_month(s):
        if s["r126"] is None or not (s["ma20"] and s["ma60"] and s["ma120"]):
            return None
        if not (s["close"] > s["ma20"] > s["ma60"] > s["ma120"]):
            return None
        return (30 * P("r63", s["r63"]) + 20 * P("r126", s["r126"])
                + 20 * near_comp(s) + 30 * sec_pct.get(s["sector"], 0.3)
                - penalty(s))

    def top(scorer, n):
        scored = [(scorer(s), s) for s in liquid]
        scored = [(v, s) for v, s in scored if v is not None]
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:n]]

    lists = {"today": top(score_today, 10), "week": top(score_week, 10),
             "month": top(score_month, 15)}

    # 직전 리스트와 비교 (신규/이탈)
    prev = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
            if old.get("tradingDate") != latest:
                prev = {k: {i["code"]: i["name"] for i in old["lists"].get(k, [])}
                        for k in ("today", "week", "month")}
        except Exception:
            pass

    def item(s, key):
        return {
            "code": s["code"], "name": s["name"], "sector": s["sector"],
            "close": s["close"],
            "r1": round((s["r1"] or 0) * 100, 2),
            "r21": round((s["r21"] or 0) * 100, 1),
            "r63": round((s["r63"] or 0) * 100, 1),
            "near52": round(s["near52"], 3),
            "newHigh": s["new_high"],
            "warn": s["limit_ups"] >= 2,
            "isNew": bool(prev) and s["code"] not in prev.get(key, {}),
            "reasons": build_reasons(s, sec_rank.get(s["sector"])),
            "spark": s["spark"],
        }

    out_lists = {k: [item(s, k) for s in v] for k, v in lists.items()}
    departed = {}
    for k in ("today", "week", "month"):
        cur = {i["code"] for i in out_lists[k]}
        departed[k] = [n for c, n in prev.get(k, {}).items() if c not in cur]

    now = datetime.now(KST)
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "tradingDate": latest,
        "universe": len(stocks),
        "sectors": [{"name": x["name"], "r5": round(x["r5"] * 100, 1),
                     "r21": round(x["r21"] * 100, 1), "n": x["n"]}
                    for x in sec_stats[:8]],
        "lists": out_lists,
        "departed": departed,
    }
    DOCS.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    ARCHIVE.mkdir(exist_ok=True)
    (ARCHIVE / f"{latest}.json").write_text(
        json.dumps({k: [(i["code"], i["name"]) for i in v] for k, v in out_lists.items()},
                   ensure_ascii=False))
    print(f"완료: {latest} 기준, 유니버스 {len(stocks)} / "
          f"오늘 {len(out_lists['today'])} · 주 {len(out_lists['week'])} · 월 {len(out_lists['month'])}")


if __name__ == "__main__":
    main()
