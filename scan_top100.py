import json
import os
import time
from datetime import datetime, timedelta, timezone
import requests

TW_TZ = timezone(timedelta(hours=8))


def map_industry(industry_str, code):
    if (code and code.startswith("00")) or industry_str == "ETF":
        return "ETF", "ETF"
    if not industry_str:
        return "TRAD", "傳產其他"
    if "金融" in industry_str:
        return "FIN", "金融股"
    if any(k in industry_str for k in ["半導體", "電子", "電腦", "光電", "通信", "資訊"]):
        return "TECH", "科技電子"
    return "TRAD", "傳產其他"


def calc_ma(arr, period):
    if len(arr) < period:
        return None
    return round(sum(arr[-period:]) / period, 2)


def calc_rsi(arr, period=6):
    if len(arr) <= period:
        return None
    gains, losses = 0.0, 0.0
    for j in range(len(arr) - period, len(arr)):
        d = arr[j] - arr[j - 1]
        if d > 0:
            gains += d
        else:
            losses += abs(d)
    if losses == 0:
        return 50.0 if gains == 0 else 100.0
    rs = gains / losses
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def calc_kd(closes, highs, lows, period=9):
    k, d = 50.0, 50.0
    for i in range(len(closes)):
        start = max(0, i - period + 1)
        low = min(lows[start : i + 1])
        high = max(highs[start : i + 1])
        rsv = 50.0 if high == low else ((closes[i] - low) / (high - low)) * 100.0
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
    return round(k, 1), round(d, 1)


def fetch_stock_info():
    meta = {}
    try:
        r = requests.get("https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo", timeout=20)
        for row in r.json().get("data", []):
            code = str(row.get("stock_id", "")).strip()
            if code:
                cat, sector = map_industry(row.get("industry_category", ""), code)
                meta[code] = {"name": row.get("stock_name", code), "cat": cat, "sector": sector}
    except Exception:
        pass
    return meta


def fetch_twse_top100():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    rows = r.json()
    valid = []
    for row in rows:
        code = str(row.get("Code", "")).strip()
        if not code or len(code) > 6 or (len(code) == 4 and not code.isdigit()):
            continue
        try:
            tv = float(str(row.get("TradeValue", "0")).replace(",", ""))
            cp = float(str(row.get("ClosingPrice", "0")).replace(",", ""))
            vol = float(str(row.get("TradeVolume", "0")).replace(",", ""))
        except ValueError:
            continue
        if cp > 0 and tv > 0:
            valid.append({"code": code, "name": row.get("Name", code), "trade_value": tv, "close": cp, "volume_lots": round(vol / 1000)})
    valid.sort(key=lambda x: x["trade_value"], reverse=True)
    return valid[:100]


def analyze_stock(code, fallback_name, meta, start_date):
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={code}&start_date={start_date}"
    r = requests.get(url, timeout=15)
    rows = [x for x in r.json().get("data", []) if float(x.get("close", 0)) > 0 and x.get("date")]
    rows.sort(key=lambda x: x["date"])
    if len(rows) < 10:
        return None

    closes = [float(x["close"]) for x in rows]
    highs = [float(x.get("max") or x["close"]) for x in rows]
    lows = [float(x.get("min") or x["close"]) for x in rows]
    vols = [float(x.get("Trading_Volume", 0)) for x in rows]
    curr = closes[-1]
    prev = closes[-2]
    chg = round(curr - prev, 2)
    chg_pct = round((chg / prev) * 100, 2) if prev else 0.0
    vol_lots = round(vols[-1] / 1000)

    # Volume-price signal (today vs prior 5-day avg volume)
    vol_signal = ""
    if len(vols) >= 6:
        avg5_vol = sum(vols[-6:-1]) / 5.0
        if avg5_vol > 0:
            ratio = vols[-1] / avg5_vol
            if ratio >= 1.5 and chg_pct <= 0.5:
                vol_signal = f"⚠️ 放量滯漲 ({ratio:.1f}x)"
            elif ratio >= 1.5 and chg_pct > 0.5:
                vol_signal = f"放量上攻 ({ratio:.1f}x)"
            elif ratio <= 0.7:
                vol_signal = f"縮量整理 ({ratio:.1f}x)"

    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    ma240 = calc_ma(closes, 240)
    rsi = calc_rsi(closes, 6)
    k_val, d_val = calc_kd(closes, highs, lows, 9)

    recent_slice = closes[-20:]
    short_slice = closes[-10:]
    recent_low, short_low, recent_high = min(recent_slice), min(short_slice), max(recent_slice)
    vol_pct = min(0.08, max(0.015, ((recent_high - recent_low) / recent_low) * 0.25)) if recent_low > 0 else 0.025

    if ma20 and curr > ma20:
        buy_high = round(ma20 * (1 + vol_pct * 0.4), 1)
        buy_low = round(ma20 * (1 - vol_pct * 0.8), 1)
        if buy_high >= curr:
            buy_high = round(curr * 0.985, 1)
    else:
        buy_high = round(curr * (1 - vol_pct * 0.5), 1)
        buy_low = round(max(short_low * 0.98, curr * (1 - vol_pct * 1.5)), 1)

    safe_low, safe_high = min(buy_low, buy_high), max(buy_low, buy_high)
    target_sell = round(max(recent_high * 1.02, curr * 1.07), 1)
    stop_loss = round(safe_low * 0.95, 1)

    score = 20
    if ma240 and curr >= ma240:
        score += 25
    elif ma240 and curr >= ma240 * 0.95:
        score += 10
    dist_pct = (curr - safe_high) / curr if curr > 0 else 0.1
    if dist_pct <= 0.03:
        score += 25
    elif dist_pct <= 0.06:
        score += 15
    if k_val > d_val:
        score += 15
    if rsi is not None and rsi <= 60:
        score += 15
    score = min(100, max(10, round(score)))

    info = meta.get(code)
    if info:
        name, cat, sector = info["name"], info["cat"], info["sector"]
    else:
        cat, sector = map_industry("", code)
        name = fallback_name

    etf_mode = code.startswith("00") or sector == "ETF"
    reasons = []
    if ma240:
        reasons.append(f"站穩年線 ({ma240}) 之上" if curr >= ma240 else f"位於年線 ({ma240}) 下方築底")
    if ma60:
        reasons.append(f"守住季線 ({ma60}) 支撐" if curr >= ma60 else f"回測季線 ({ma60}) 下方")
    reasons.append(f"KD 黃金交叉 ({k_val}/{d_val})" if k_val > d_val else f"KD 偏弱整理 ({k_val}/{d_val})")
    if vol_signal:
        reasons.append(vol_signal)
    elif rsi is not None:
        reasons.append(f"RSI {rsi}")

    trend_label = "區間整理"
    if ma20 and ma60 and curr > ma20 and ma20 > ma60:
        trend_label = "強勢多頭"
    elif ma240 and curr < ma240:
        trend_label = "年線下築底"
    elif curr <= safe_high * 1.02:
        trend_label = "接近支撐區"

    return {
        "code": code,
        "name": name,
        "cat": cat,
        "sector": sector,
        "etfMode": etf_mode,
        "price": curr,
        "chg": chg,
        "chgPct": chg_pct,
        "volumeLots": vol_lots,
        "volSignal": vol_signal,
        "aboveMa20": bool(ma20 and curr >= ma20),
        "lastDate": rows[-1]["date"],
        "ma20": ma20,
        "ma60": ma60,
        "ma240": ma240,
        "rsi": rsi,
        "k": k_val,
        "d": d_val,
        "safeBuyLow": safe_low,
        "safeBuyHigh": safe_high,
        "targetSell": target_sell,
        "stopLoss": stop_loss,
        "score": score,
        "trendLabel": trend_label,
        "reasonText": " · ".join(reasons[:3]),
    }



def fetch_macro_snapshot(now_tw):
    start_35 = (now_tw - timedelta(days=35)).strftime("%Y-%m-%d")
    start_14 = (now_tw - timedelta(days=14)).strftime("%Y-%m-%d")
    macro = {}

    # 1. TAIEX + Margin Balance (15 trading days)
    taiex_rows = []
    try:
        r = requests.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id=TAIEX&start_date={start_35}",
            timeout=20,
        )
        taiex_rows = [
            x for x in r.json().get("data", []) if float(x.get("close", 0)) > 0 and x.get("date")
        ]
        taiex_rows.sort(key=lambda x: x["date"])
    except Exception:
        pass

    margin_map = {}
    try:
        r = requests.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTotalMarginPurchaseShortSale&start_date={start_35}",
            timeout=20,
        )
        for row in r.json().get("data", []):
            if row.get("date") and row.get("name") == "MarginPurchaseMoney" and float(row.get("TodayBalance", 0)) > 0:
                raw_val = float(row["TodayBalance"])
                billion_val = raw_val / 1e8 if raw_val > 1e10 else raw_val / 1e5
                margin_map[row["date"]] = round(billion_val, 2)
    except Exception:
        pass

    if taiex_rows:
        last_t = taiex_rows[-1]
        macro["taiexClose"] = float(last_t["close"])
        macro["taiexSpread"] = float(last_t.get("spread", 0) or 0)
        macro["taiexMoneyBillion"] = round(float(last_t.get("Trading_money", 0) or 0) / 1e8, 1)
        macro["latestMarketDate"] = last_t["date"]

        recent15 = taiex_rows[-15:]
        dates = [x["date"] for x in recent15]
        closes = [float(x["close"]) for x in recent15]
        last_m = None
        margins = []
        for d in dates:
            if d in margin_map:
                last_m = margin_map[d]
            margins.append(last_m)
        macro["taiexSeries"] = {
            "dates": dates,
            "labels": [d[5:] for d in dates],
            "closes": closes,
            "marginBillion": margins,
        }

    # 2. TX Futures (Day & Night)
    try:
        r = requests.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesDaily&data_id=TX&start_date={start_14}",
            timeout=20,
        )
        valid_tx = [
            x
            for x in r.json().get("data", [])
            if float(x.get("close", 0)) > 0 and "/" not in str(x.get("contract_date", ""))
        ]

        def pick_latest(rows):
            if not rows:
                return None
            max_d = max(x["date"] for x in rows)
            same_day = [x for x in rows if x["date"] == max_d]
            same_day.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)
            return same_day[0]

        day_last = pick_latest([x for x in valid_tx if x.get("trading_session") in ("position", "", None)])
        if day_last:
            tx_c = float(day_last["close"])
            macro["txClose"] = tx_c
            macro["txDaySpread"] = float(day_last.get("spread", 0) or 0)
            macro["txDayDate"] = day_last["date"]
            if "taiexClose" in macro:
                macro["txSpread"] = round(tx_c - macro["taiexClose"], 2)

        night_last = pick_latest([x for x in valid_tx if x.get("trading_session") == "after_market"])
        if night_last:
            macro["txNightClose"] = float(night_last["close"])
            macro["txNightSpread"] = float(night_last.get("spread", 0) or 0)
            macro["txNightDate"] = night_last["date"]
    except Exception:
        pass

    # 3. USD/TWD Exchange Rate
    try:
        r = requests.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanExchangeRate&data_id=USD&start_date={start_14}",
            timeout=20,
        )
        fx_rows = sorted([x for x in r.json().get("data", []) if x.get("date")], key=lambda x: x["date"])
        if fx_rows:
            last_fx = fx_rows[-1]
            sb = float(last_fx.get("spot_buy", 0) or 0)
            ss = float(last_fx.get("spot_sell", 0) or 0)
            if sb > 0 and ss > 0:
                macro["usdTwd"] = round((sb + ss) / 2.0, 3)
                macro["fxSpotBuy"] = sb
                macro["fxSpotSell"] = ss
                macro["fxDate"] = last_fx["date"]
    except Exception:
        pass

    # 4. Market-wide Institutional Investors (15 trading days)
    try:
        r = requests.get(
            f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTotalInstitutionalInvestors&start_date={start_35}",
            timeout=20,
        )
        by_date = {}
        for row in r.json().get("data", []):
            d = row.get("date")
            if not d:
                continue
            if d not in by_date:
                by_date[d] = {"foreign": 0.0, "trust": 0.0, "dealer": 0.0}
            net_b = (float(row.get("buy", 0) or 0) - float(row.get("sell", 0) or 0)) / 1e8
            name = row.get("name", "")
            if "Foreign" in name:
                by_date[d]["foreign"] += net_b
            elif "Investment_Trust" in name:
                by_date[d]["trust"] += net_b
            elif "Dealer" in name:
                by_date[d]["dealer"] += net_b
        dates = sorted(by_date.keys())[-15:]
        if dates:
            foreign = [round(by_date[d]["foreign"], 2) for d in dates]
            trust = [round(by_date[d]["trust"], 2) for d in dates]
            dealer = [round(by_date[d]["dealer"], 2) for d in dates]
            total = [round(foreign[i] + trust[i] + dealer[i], 2) for i in range(len(dates))]
            macro["marketChips"] = {
                "dates": dates,
                "labels": [d[5:] for d in dates],
                "foreign": foreign,
                "trust": trust,
                "dealer": dealer,
                "total": total,
            }
    except Exception:
        pass

    return macro


def main():
    now_tw = datetime.now(TW_TZ)
    start_date = (now_tw - timedelta(days=400)).strftime("%Y-%m-%d")
    meta = fetch_stock_info()
    top100_raw = fetch_twse_top100()

    results = []
    for idx, item in enumerate(top100_raw, 1):
        try:
            analyzed = analyze_stock(item["code"], item["name"], meta, start_date)
            if analyzed:
                analyzed["volumeRank"] = idx
                results.append(analyzed)
        except Exception:
            pass
        time.sleep(0.35)

    results.sort(key=lambda x: x["score"], reverse=True)
    total = len(results)
    breadth = {
        "total": total,
        "upCount": sum(1 for x in results if x["chg"] > 0),
        "aboveMa20Count": sum(1 for x in results if x.get("aboveMa20")),
        "avgChgPct": round(sum(x["chgPct"] for x in results) / total, 2) if total else 0.0,
    }
    macro = fetch_macro_snapshot(now_tw)
    payload = {
        "updatedAt": now_tw.strftime("%Y-%m-%d %H:%M"),
        "count": total,
        "breadth": breadth,
        "macro": macro,
        "items": results,
    }
    with open("top100.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Generated top100.json with {total} stocks, breadth={breadth}")


if __name__ == "__main__":
    main()