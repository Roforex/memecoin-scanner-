"""Dip and Reclaim: paper-trading bot for Solana memecoins. Pretend money only.

Don't buy the pump. Wait for a coin that already proved real interest to pull back,
calm down, then buy when it breaks back up.

Every run: watch safe coins (liq >= $40k, mcap $100k-$20M), load 5-minute candles and look for:
  pump 100%+, pullback 30-60%, 30 min calm (12% range), reclaim on 1.5x volume.
Paper buy $10. Stop under the calm range (max 20%). Half at +50% (stop to break-even),
rest at +100%, or after 48h. 3% slippage + 1% fee each side. Max 3 open, 5 new per day.
"""
import html
import sys
import time

import requests

import bot

STAKE = 10.0
SLIP, FEE = 0.03, 0.01
WATCH_HOURS = 48
MAX_OPEN, MAX_PER_DAY = 3, 5
CANDLE_CALLS_PER_RUN = 24
GT = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"

PUMP_X = 2.0
DIP_MIN, DIP_MAX = 0.30, 0.60
CALM_BARS, CALM_RANGE = 6, 0.12
VOL_X = 1.5
STOP_BUF, MAX_STOP = 0.02, 0.20
TP1, TP2, TP1_SHARE = 0.50, 1.00, 0.5
MAX_HOLD_H = 48

log = bot.log


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dip_watch (
        mint TEXT PRIMARY KEY, symbol TEXT, pool TEXT, first_seen INTEGER,
        safe INTEGER, last_checked INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dip (
        mint TEXT PRIMARY KEY, symbol TEXT, pool TEXT, opened_at INTEGER, entry REAL, stop REAL,
        took_tp1 INTEGER DEFAULT 0, status TEXT, closed_at INTEGER, pnl REAL, reason TEXT,
        checked_to INTEGER)""")
    db.commit()


def candles(pool, limit=300):
    for attempt in range(3):
        try:
            r = bot.session.get(GT.format(pool=pool), timeout=15,
                                params={"aggregate": 5, "limit": limit, "currency": "usd"},
                                headers={"Accept": "application/json;version=20230302"})
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1)); continue
            if r.status_code != 200:
                return None
            rows = r.json()["data"]["attributes"]["ohlcv_list"]
            rows = sorted((tuple(float(x) for x in row) for row in rows), key=lambda x: x[0])
            now = time.time()
            return [x for x in rows if x[0] + 300 <= now]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            time.sleep(2)
    return None


def find_setup(cs):
    if len(cs) < 40:
        return None, None, "not enough candles"
    hi = [x[2] for x in cs]; lo = [x[3] for x in cs]; cl = [x[4] for x in cs]; vol = [x[5] for x in cs]
    last = len(cs) - 1
    calm = range(last - CALM_BARS, last)
    pre = range(0, last - CALM_BARS)
    peak_i = max(pre, key=lambda i: hi[i])
    peak = hi[peak_i]
    if peak_i == 0:
        return None, None, "no pump in view"
    base = min(lo[:peak_i])
    if base <= 0 or peak < PUMP_X * base:
        return None, None, f"no 100% pump (x{peak / base:.1f})" if base > 0 else "bad data"
    dip_low = min(lo[peak_i:last])
    dd = 1 - dip_low / peak
    if not DIP_MIN <= dd <= DIP_MAX:
        return None, None, f"pullback {dd:.0%} not in 30-60%"
    r_hi = max(hi[i] for i in calm); r_lo = min(lo[i] for i in calm)
    if (r_hi - r_lo) / r_lo > CALM_RANGE:
        return None, None, "not calm yet"
    avg_v = sum(vol[i] for i in calm) / CALM_BARS
    if cl[last] <= r_hi:
        return None, None, "no reclaim"
    if avg_v <= 0 or vol[last] < VOL_X * avg_v:
        return None, None, "reclaim on weak volume"
    entry = cl[last]
    stop = r_lo * (1 - STOP_BUF)
    if (entry - stop) / entry > MAX_STOP:
        return None, None, "stop too far"
    return entry, stop, f"pump x{peak / base:.1f}, dip {dd:.0%}, reclaim on {vol[last] / avg_v:.1f}x vol"


def proceeds(entry, exits):
    tokens = STAKE * (1 - FEE) / (entry * (1 + SLIP))
    return sum(share * tokens * px * (1 - SLIP) * (1 - FEE) for share, px in exits)


def manage(cfg, db, now):
    rows = db.execute("SELECT mint, symbol, pool, opened_at, entry, stop, took_tp1, checked_to "
                      "FROM dip WHERE status='open'").fetchall()
    for mint, sym, pool, opened, entry, stop, took, checked_to in rows:
        cs = candles(pool)
        if cs is None:
            continue
        new = [x for x in cs if x[0] >= (checked_to or opened)]
        exits, reason = [], None
        if took:
            exits.append((TP1_SHARE, entry * (1 + TP1)))
        cur_stop = entry if took else stop
        for ts, o, h, l, c, v in new:
            if l <= cur_stop:
                px = min(o, cur_stop)
                exits.append((1 - TP1_SHARE if took else 1.0, px))
                reason = "break-even stop" if took else "stop loss"
                break
            if not took and h >= entry * (1 + TP1):
                took = 1; cur_stop = entry
                exits = [(TP1_SHARE, entry * (1 + TP1))]
                db.execute("UPDATE dip SET took_tp1=1 WHERE mint=?", (mint,))
                bot.send_telegram(cfg, f"💰 <b>Dip paper TP1</b> ${html.escape(sym)}: sold half at +50%, "
                                       f"stop moved to break-even.")
            if took and h >= entry * (1 + TP2):
                exits.append((1 - TP1_SHARE, entry * (1 + TP2)))
                reason = "target +100%"
                break
        if reason is None and now - opened >= MAX_HOLD_H * 3600 and cs:
            exits.append((1 - TP1_SHARE if took else 1.0, cs[-1][4]))
            reason = "48h time limit"
        if reason is None:
            if cs:
                db.execute("UPDATE dip SET checked_to=? WHERE mint=?", (int(cs[-1][0]) + 300, mint))
            continue
        pnl = proceeds(entry, exits) - STAKE
        db.execute("UPDATE dip SET status='closed', closed_at=?, pnl=?, reason=? WHERE mint=?",
                   (int(now), pnl, reason, mint))
        icon = "🟢" if pnl > 0 else "🔴"
        bot.send_telegram(cfg, f"{icon} <b>Dip paper sell</b> ${html.escape(sym)} · {reason}\n"
                               f"P&amp;L {pnl:+.2f} USD ({pnl / STAKE * 100:+.0f}% after fees)")
    db.commit()


def scan(cfg, db, now):
    for m in bot.discover_new_mints():
        db.execute("INSERT OR IGNORE INTO dip_watch (mint, first_seen) VALUES (?, ?)", (m, int(now)))
    db.execute("DELETE FROM dip_watch WHERE first_seen < ?", (int(now - WATCH_HOURS * 3600),))
    db.execute("DELETE FROM dip_watch WHERE mint IN (SELECT mint FROM dip)")
    db.commit()
    watch = [r[0] for r in db.execute("SELECT mint FROM dip_watch WHERE safe IS NOT 0")]
    pairs, _ = bot.fetch_pairs(watch)
    eligible = []
    for mint, p in pairs.items():
        liq = bot._liq(p)
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        if liq < 40000 or not 100000 <= mcap <= 20000000 or not p.get("pairAddress"):
            continue
        eligible.append((mint, p))
    open_n = db.execute("SELECT COUNT(*) FROM dip WHERE status='open'").fetchone()[0]
    today = db.execute("SELECT COUNT(*) FROM dip WHERE opened_at >= ?", (int(now - 86400),)).fetchone()[0]
    checked = dict(db.execute("SELECT mint, last_checked FROM dip_watch"))
    eligible.sort(key=lambda mp: checked.get(mp[0]) or 0)
    calls = safety_calls = 0
    for mint, p in eligible:
        if calls >= CANDLE_CALLS_PER_RUN or open_n >= MAX_OPEN or today >= MAX_PER_DAY:
            break
        sym = (p.get("baseToken") or {}).get("symbol") or "?"
        row = db.execute("SELECT safe FROM dip_watch WHERE mint=?", (mint,)).fetchone()
        if row is None:
            continue
        safe = row[0]
        if safe is None:
            if safety_calls >= 15:
                continue
            safety_calls += 1
            reasons, _ = bot.check_safety(cfg, mint, p)
            safe = 0 if reasons else 1
            db.execute("UPDATE dip_watch SET safe=?, symbol=?, pool=? WHERE mint=?",
                       (safe, sym, p["pairAddress"], mint))
            if not safe:
                log.info("unsafe $%s: %s", sym, "; ".join(reasons)); continue
        elif not safe:
            continue
        cs = candles(p["pairAddress"]); calls += 1
        time.sleep(2.1)
        db.execute("UPDATE dip_watch SET last_checked=? WHERE mint=?", (int(now), mint))
        if not cs:
            continue
        entry, stop, why = find_setup(cs)
        log.info("$%s: %s", sym, why)
        if entry is None:
            continue
        db.execute("INSERT OR IGNORE INTO dip (mint, symbol, pool, opened_at, entry, stop, status, "
                   "checked_to) VALUES (?,?,?,?,?,?, 'open', ?)",
                   (mint, sym, p["pairAddress"], int(now), entry, stop, int(cs[-1][0]) + 300))
        open_n += 1; today += 1
        e = html.escape
        bot.send_telegram(cfg, f"📉➡️📈 <b>Dip paper buy</b> ${e(sym)} · $10 at {entry:.8g}\n"
                               f"{e(why)}\nStop {stop:.8g} ({(stop / entry - 1) * 100:.0f}%) · "
                               f"half at +50%, rest at +100%. Pretend money only.\n"
                               f'<a href="{e(p.get("url", ""))}">Chart</a>')
    db.commit()
    log.info("watching %d, eligible %d, candle checks %d, open %d", len(watch), len(eligible), calls, open_n)


def report(db):
    rows = db.execute("SELECT symbol, pnl, reason FROM dip WHERE status='closed'").fetchall()
    open_n = db.execute("SELECT COUNT(*) FROM dip WHERE status='open'").fetchone()[0]
    print("DIP AND RECLAIM, paper trading (pretend $10 per trade)")
    print(f"{len(rows)} closed trades, {open_n} still open\n")
    if not rows:
        return
    total = sum(r[1] for r in rows)
    wins = sum(1 for r in rows if r[1] > 0)
    print(f"Total P&L:  {total:+.2f} USD on {len(rows) * STAKE:.0f} USD staked "
          f"({total / (len(rows) * STAKE) * 100:+.0f}%)")
    print(f"Win rate:   {wins / len(rows) * 100:.0f}% ({wins} of {len(rows)})")
    by = {}
    for _, pnl, reason in rows:
        by.setdefault(reason, []).append(pnl)
    for reason, v in sorted(by.items()):
        print(f"{reason:<18}{len(v):>4} trades {sum(v):>+9.2f} USD")


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = bot.load_config("config.toml")
    db = bot.open_db(cfg["scanner"]["db_path"])
    tables(db)
    if "--report" in sys.argv:
        report(db); return
    now = time.time()
    manage(cfg, db, now)
    scan(cfg, db, now)


if __name__ == "__main__":
    main()
