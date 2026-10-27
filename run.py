"""Stricter scanner. Runs bot.py with two upgrades:
1. Skips coins younger than min_pair_age_hours (most dumps happen early).
2. Gives the price checks more time, since GitHub often runs late.
"""
import time

import bot

_original_check_market = bot.check_market


def check_market(pair, f, now_ms):
    reasons = _original_check_market(pair, f, now_ms)
    age = bot.pair_age_hours(pair, now_ms)
    min_age = f.get("min_pair_age_hours", 0)
    if age is not None and age < min_age:
        reasons.append(f"too new ({age:.1f}h, waiting for {min_age}h)")
    return reasons


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


bot.check_market = check_market
bot.update_followups = update_followups

if __name__ == "__main__":
    bot.main()
