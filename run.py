"""Stricter scanner + paper trading. Runs bot.py with three upgrades:
1. Skips coins younger than min_pair_age_hours (most dumps happen early).
2. Gives the price checks more time, since GitHub often runs late.
3. Paper trading: every alert gets a pretend buy, sold later by fixed rules.
   No real money, no wallet. Results: python run.py --report
"""
import sys
import time

import bot

CFG = {}
_original_check_market = bot.check_market
_original_record_alert = bot.record_alert
_original_scan_cycle = bot.scan_cycle


# ---------------- 1. minimum age

def check_market(pair, f, now_ms):
    reasons = _original_check_market(pair, f, now_ms)
    age = bot.pair_age_hours(pair, now_ms)
    min_age = f.get("min_pair_age_hours", 0)
    if age is not None and age < min_age:
        reasons.append(f"too new ({age:.1f}h, waiting for {min_age}h)")
    return reasons


# ---------------- 2. more patient price checks

def update_followups(db, now=None):
    now = int(now or time.time())
    rows = db.execute("SELECT mint, alerted_at, price_15m, price_1h, price_4h, price_24h "
                      "FROM alerts WHERE price_24h IS NULL").fetchall()
    due = {}
    for mint, at, *vals in rows:
        for (col, secs), val in zip(bot.CHECKPOINTS.items(), vals):
            if val is None and now - at >= secs:
                late = now - at > secs + max(1800, secs * 0.5)
                due.setdefault(mint, []).append((col, late))
    live = [m for m, cols in due.items() if any(not late for _, late in cols)]
    pairs, ok = bot.fetch_pairs(live) if live else ({}, True)
    for mint, cols in due.items():
        for col, late in cols:
            if late:
                value = bot.MISSED
            elif not ok:
                continue
            else:
                p = pairs.get(mint)
                value = float(p.get("priceUsd") or 0) if p else 0.0
            db.execute(f"UPDATE alerts SET {col}=? WHERE mint=?", (value, mint))
    db.commit()
    paper_manage(db, now)


# ---------------- 3. paper trading

def paper_cfg():
    p = {"enabled": True, "stake_usd": 10, "take_profit_pct": 100, "stop_loss_pct": 30,
         "max_hold_hours": 24, "slippage_pct": 3, "fee_pct": 1}
    p.update(CFG.get("paper") or {})
    return p


def paper_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS paper (
        mint TEXT PRIMARY KEY, symbol TEXT, opened_at INTEGER, entry REAL, stake REAL,
        status TEXT, closed_at INTEGER, exit REAL, reason TEXT, pnl REAL)""")


def paper_proceeds(stake, entry, exit_price, p):
    if entry <= 0 or exit_price <= 0:
        return 0.0
    slip, fee = p["slippage_pct"] / 100, p["fee_pct"] / 100
    tokens = stake * (1 - fee) / (entry * (1 + slip))
    return tokens * exit_price * (1 - slip) * (1 - fee)


def record_alert(db, pair, now):
    _original_record_alert(db, pair, now)
    p = paper_cfg()
    price = float(pair.get("priceUsd") or 0)
    if not p["enabled"] or price <= 0:
        return
    paper_table(db)
    symbol = (pair.get("baseToken") or {}).get("symbol")
    mint = (pair.get("baseToken") or {}).get("address")
    cur = db.execute("INSERT OR IGNORE INTO paper (mint, symbol, opened_at, entry, stake, status) "
                     "VALUES (?,?,?,?,?, 'open')", (mint, symbol, int(now), price, p["stake_usd"]))
    db.commit()
    if cur.rowcount:
        bot.send_telegram(CFG, f"📝 <b>Paper buy</b> ${bot.html.escape(str(symbol))} · "
                               f"${p['stake_usd']:.0f} at {price:.8g}\n"
                               f"Sells at +{p['take_profit_pct']}% or -{p['stop_loss_pct']}% "
                               f"or after {p['max_hold_hours']}h. Pretend money only.")


def paper_manage(db, now):
    p = paper_cfg()
    if not p["enabled"]:
        return
    paper_table(db)
    rows = db.execute("SELECT mint, symbol, opened_at, entry, stake FROM paper "
                      "WHERE status='open'").fetchall()
    if not rows:
        return
    pairs, ok = bot.fetch_pairs([r[0] for r in rows])
    if not ok:
        return
    for mint, symbol, opened_at, entry, stake in rows:
        pair = pairs.get(mint)
        price = float(pair.get("priceUsd") or 0) if pair else 0.0
        change = (price / entry - 1) * 100 if entry else -100
        held_h = (now - opened_at) / 3600
        if price <= 0:
            reason = "gone (treated as rug)"
        elif change >= p["take_profit_pct"]:
            reason = "take profit"
        elif change <= -p["stop_loss_pct"]:
            reason = "stop loss"
        elif held_h >= p["max_hold_hours"]:
            reason = "time limit"
        else:
            continue
        pnl = paper_proceeds(stake, entry, price, p) - stake
        db.execute("UPDATE paper SET status='closed', closed_at=?, exit=?, reason=?, pnl=? "
                   "WHERE mint=?", (int(now), price, reason, pnl, mint))
        db.commit()
        icon = "🟢" if pnl > 0 else "🔴"
        bot.send_telegram(CFG, f"{icon} <b>Paper sell</b> ${bot.html.escape(str(symbol))} · "
                               f"{reason} after {held_h:.1f}h\n"
                               f"Price {change:+.0f}% · P&amp;L {pnl:+.2f} USD "
                               f"({pnl / stake * 100:+.0f}% after fees)")


def paper_report(db):
    paper_table(db)
    closed = db.execute("SELECT symbol, pnl, stake, reason FROM paper WHERE status='closed' "
                        "ORDER BY closed_at").fetchall()
    open_n = db.execute("SELECT COUNT(*) FROM paper WHERE status='open'").fetchone()[0]
    print("\nPAPER TRADING (pretend money)")
    print(f"{len(closed)} closed trades, {open_n} still open\n")
    if not closed:
        return
    total = sum(r[1] for r in closed)
    staked = sum(r[2] for r in closed)
    wins = [r for r in closed if r[1] > 0]
    print(f"Total P&L:   {total:+.2f} USD on {staked:.0f} USD staked ({total / staked * 100:+.0f}%)")
    print(f"Win rate:    {len(wins) / len(closed) * 100:.0f}% ({len(wins)} of {len(closed)})")
    best, worst = max(closed, key=lambda r: r[1]), min(closed, key=lambda r: r[1])
    print(f"Best trade:  ${best[0]} {best[1]:+.2f} USD")
    print(f"Worst trade: ${worst[0]} {worst[1]:+.2f} USD\n")
    reasons = {}
    for r in closed:
        reasons.setdefault(r[3], []).append(r[1])
    for reason, pnls in sorted(reasons.items()):
        print(f"{reason:<22}{len(pnls):>4} trades {sum(pnls):>+9.2f} USD")
    print("\nIncludes slippage and fees. Real results can still be worse.")


# ---------------- wiring

def scan_cycle(cfg, db, state):
    CFG.clear()
    CFG.update(cfg)
    _original_scan_cycle(cfg, db, state)


bot.check_market = check_market
bot.update_followups = update_followups
bot.record_alert = record_alert
bot.scan_cycle = scan_cycle

if __name__ == "__main__":
    report_mode = "--report" in sys.argv
    if report_mode:
        CFG.update(bot.load_config("config.toml"))
    bot.main()
    if report_mode:
        paper_report(bot.open_db(CFG["scanner"]["db_path"]))
