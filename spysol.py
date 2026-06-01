import os
import asyncio
import requests
import time
from datetime import datetime, timezone
from telegram import Bot
from telegram.constants import ParseMode

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

CONFIG = {
    "MAX_MC":        100_000_000,
    "SCAN_INTERVAL": 120,           # seconds between scans
}

seen: set = set()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SEARCH_TERMS = [
    "pump", "meme", "pepe", "inu", "doge", "moon", "sol",
    "ai", "elon", "baby", "shib", "floki", "chad", "wojak",
]


# ── Data fetching (sync, runs in thread pool) ─────────────────────────────────

def _fetch_dex_search(chain):
    pairs = []
    for term in SEARCH_TERMS:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/search?q={term}",
                timeout=10, headers=HEADERS,
            )
            for p in r.json().get("pairs", []):
                if p.get("chainId") == chain:
                    pairs.append(p)
        except Exception:
            pass
    return pairs


def _fetch_dex_latest(chain):
    pairs = []
    addresses = []
    for endpoint in [
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]:
        try:
            r = requests.get(endpoint, timeout=10, headers=HEADERS)
            items = r.json() if isinstance(r.json(), list) else []
            addresses += [
                i["tokenAddress"] for i in items
                if i.get("chainId") == chain and i.get("tokenAddress")
            ]
        except Exception:
            pass
    for addr in list(set(addresses))[:20]:
        try:
            r2 = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
                timeout=8, headers=HEADERS,
            )
            pairs += r2.json().get("pairs", [])
            time.sleep(0.05)
        except Exception:
            pass
    return pairs


def _get_all_pairs():
    pairs = []
    for chain in ["solana", "base"]:
        pairs += _fetch_dex_search(chain)
        pairs += _fetch_dex_latest(chain)
    # Deduplicate by base token address
    seen_addrs: set = set()
    unique = []
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        if addr and addr not in seen_addrs:
            seen_addrs.add(addr)
            unique.append(p)
    return unique


# ── Analysis ──────────────────────────────────────────────────────────────────

def _token_age_label(pair):
    created = pair.get("pairCreatedAt")
    if not created:
        return "unknown"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    secs = (now_ms - int(created)) / 1000
    if secs < 0:
        return "unknown"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        m, s = divmod(int(secs), 60)
        return f"{m}m {s}s"
    return f"{int(secs // 3600)}h"


def _analyze(pair):
    try:
        base = pair.get("baseToken") or {}
        addr = base.get("address", "")
        if not addr or addr in seen:
            return None
        mc  = float(pair.get("fdv") or pair.get("marketCap") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol = float((pair.get("volume") or {}).get("h1") or 0)
        chg = float((pair.get("priceChange") or {}).get("h1") or 0)
        if mc > CONFIG["MAX_MC"]:
            return None
        seen.add(addr)
        return {
            "name":      base.get("name", "Unknown"),
            "symbol":    base.get("symbol", "???"),
            "addr":      addr,
            "mc":        mc,
            "liq":       liq,
            "vol":       vol,
            "chg":       chg,
            "age_label": _token_age_label(pair),
            "chain":     pair.get("chainId", "solana"),
        }
    except Exception:
        return None


def _fmt(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1000:.1f}K"
    return f"${n:.0f}"


# ── Telegram (async) ──────────────────────────────────────────────────────────

async def send_alert(bot: Bot, t: dict):
    chg_str = f"+{t['chg']:.1f}%" if t["chg"] >= 0 else f"{t['chg']:.1f}%"
    msg = (
        f"🚨 *{t['name']}* (${t['symbol']})\n"
        f"⛓ {t['chain'].upper()}  🕐 {t['age_label']} old\n\n"
        f"📋 `{t['addr']}`\n\n"
        f"💰 MC: {_fmt(t['mc'])}\n"
        f"💧 Liq: {_fmt(t['liq'])}\n"
        f"📊 Vol 1H: {_fmt(t['vol'])}\n"
        f"📈 Change: {chg_str}\n\n"
        f"🛡️ [RugCheck](https://rugcheck.xyz/tokens/{t['addr']})  "
        f"🐦 [Birdeye](https://birdeye.so/token/{t['addr']})"
    )
    await bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )
    print(f"[{datetime.now():%H:%M:%S}] ✅ {t['name']} ({t['symbol']}) — {t['age_label']} old")


# ── Main async loop ───────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)

    # Warm up: cache current tokens, don't alert on them
    print(f"[{datetime.now():%H:%M:%S}] ⏳ Warming up...")
    loop = asyncio.get_running_loop()
    pairs = await loop.run_in_executor(None, _get_all_pairs)
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        if addr:
            seen.add(addr)
    print(f"    Warm-up done — {len(seen)} tokens cached. Scanning every "
          f"{CONFIG['SCAN_INTERVAL']//60}m for new ones...")

    while True:
        await asyncio.sleep(CONFIG["SCAN_INTERVAL"])
        print(f"[{datetime.now():%H:%M:%S}] 🔍 Scanning...")
        pairs = await loop.run_in_executor(None, _get_all_pairs)
        sent = 0
        for p in pairs:
            token = _analyze(p)
            if token:
                try:
                    await send_alert(bot, token)
                    sent += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"    Send error: {e}")
        print(f"    Fetched {len(pairs)} unique — sent {sent} alerts")


if __name__ == "__main__":
    print("🚀 Meme Coin Scanner — running!")
    asyncio.run(main())
