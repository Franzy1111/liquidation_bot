"""
Binance Futures Liquidation + Volume Spike Bot
Надсилає сигнали в Telegram коли є велика ліквідація + спалах обсягу
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime

import os

import aiohttp
import websockets

# ─── НАЛАШТУВАННЯ ────────────────────────────────────────────────
# Читаємо з Environment Variables (налаштовуються в Railway)
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Мінімальна сума ліквідації в USDT для алерту
MIN_LIQUIDATION_USDT = 50_000   # 50к USDT

# Спалах обсягу: поточна свічка більша за середню в X разів
VOLUME_SPIKE_MULTIPLIER = 3.0   # у 3 рази більше за середню

# Скільки свічок брати для підрахунку середнього обсягу
VOLUME_AVG_PERIOD = 20

# Таймфрейми для перевірки (одночасно всі три)
TIMEFRAMES = ["1m", "5m", "15m"]

# Cooldown між алертами для однієї монети (секунди)
ALERT_COOLDOWN = 300  # 5 хвилин
# ─────────────────────────────────────────────────────────────────


# Зберігаємо дані по монетах
liquidations = defaultdict(list)       # symbol -> [{"usdt": ..., "side": ..., "time": ...}]
volume_data = defaultdict(lambda: defaultdict(list))  # symbol -> tf -> [volumes]
last_alert = defaultdict(float)        # symbol -> timestamp останнього алерту


async def send_telegram(message: str):
    """Надсилає повідомлення в Telegram"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    print(f"[TG Error] {await resp.text()}")
    except Exception as e:
        print(f"[TG Exception] {e}")


async def get_all_futures_symbols() -> list[str]:
    """Отримує всі USDT-M ф'ючерсні пари з Binance"""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    symbols = [
        s["symbol"]
        for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    ]
    print(f"[Init] Знайдено {len(symbols)} активних пар")
    return symbols


async def get_klines(symbol: str, interval: str, limit: int = 25) -> list[float]:
    """Отримує останні свічки і повертає список обсягів"""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
        # Індекс 5 = quote asset volume (в USDT)
        return [float(k[7]) for k in data]
    except Exception:
        return []


def check_volume_spike(volumes: list[float]) -> tuple[bool, float]:
    """
    Перевіряє чи є спалах обсягу на останній свічці.
    Повертає (True/False, множник)
    """
    if len(volumes) < VOLUME_AVG_PERIOD + 1:
        return False, 0.0

    current = volumes[-1]
    avg = sum(volumes[-VOLUME_AVG_PERIOD - 1:-1]) / VOLUME_AVG_PERIOD

    if avg == 0:
        return False, 0.0

    multiplier = current / avg
    return multiplier >= VOLUME_SPIKE_MULTIPLIER, round(multiplier, 1)


async def check_and_alert(symbol: str, liq_usdt: float, liq_side: str):
    """
    Після ліквідації перевіряє спалах обсягу на всіх таймфреймах.
    Якщо є збіг — надсилає алерт у TG.
    """
    now = time.time()

    # Cooldown — не спамимо
    if now - last_alert[symbol] < ALERT_COOLDOWN:
        return

    # Перевіряємо всі три таймфрейми
    spike_results = {}
    for tf in TIMEFRAMES:
        volumes = await get_klines(symbol, tf)
        is_spike, mult = check_volume_spike(volumes)
        spike_results[tf] = (is_spike, mult)

    # Скільки таймфреймів підтверджують спалах?
    confirmed = [(tf, mult) for tf, (is_spike, mult) in spike_results.items() if is_spike]

    # Потрібно хоча б 2 з 3 таймфреймів
    if len(confirmed) < 2:
        return

    last_alert[symbol] = now

    # Формуємо повідомлення
    side_emoji = "🔴" if liq_side == "SELL" else "🟢"
    side_text = "LONG ліквідовано" if liq_side == "SELL" else "SHORT ліквідовано"

    tf_lines = "\n".join(
        f"  • {tf}: <b>×{mult}</b> від середнього" for tf, mult in confirmed
    )

    msg = (
        f"⚡ <b>СИГНАЛ: {symbol}</b>\n\n"
        f"{side_emoji} {side_text}\n"
        f"💥 Сума ліквідації: <b>${liq_usdt:,.0f}</b>\n\n"
        f"📊 Спалах обсягу підтверджено ({len(confirmed)}/3 TF):\n"
        f"{tf_lines}\n\n"
        f"🕐 {datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )

    print(f"[ALERT] {symbol} | Ліквідація ${liq_usdt:,.0f} | Спалах на {[tf for tf,_ in confirmed]}")
    await send_telegram(msg)


async def liquidation_stream():
    """
    Підключається до Binance WebSocket потоку всіх ліквідацій.
    Один потік — всі монети одразу.
    """
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"

    while True:
        try:
            print("[WS] Підключення до потоку ліквідацій...")
            async with websockets.connect(url, ping_interval=20) as ws:
                print("[WS] Підключено! Слухаємо ліквідації...")
                await send_telegram(
                    "✅ <b>Бот запущено!</b>\n\n"
                    f"🔍 Мінімальна ліквідація: ${MIN_LIQUIDATION_USDT:,}\n"
                    f"📈 Множник спалаху: ×{VOLUME_SPIKE_MULTIPLIER}\n"
                    f"⏱ Таймфрейми: {', '.join(TIMEFRAMES)}"
                )

                async for raw in ws:
                    data = json.loads(raw)

                    # Формат: {"o": {...}} або {"data": {...}}
                    order = data.get("o") or data.get("data", {}).get("o", {})
                    if not order:
                        continue

                    symbol = order.get("s", "")
                    side = order.get("S", "")      # BUY або SELL
                    qty = float(order.get("q", 0))
                    price = float(order.get("ap", 0) or order.get("p", 0))
                    liq_usdt = qty * price

                    if liq_usdt < MIN_LIQUIDATION_USDT:
                        continue

                    print(f"[LIQ] {symbol} | {side} | ${liq_usdt:,.0f}")

                    # Запускаємо перевірку обсягу асинхронно
                    asyncio.create_task(check_and_alert(symbol, liq_usdt, side))

        except Exception as e:
            print(f"[WS Error] {e} — перепідключення через 5с...")
            await asyncio.sleep(5)


async def main():
    print("=" * 50)
    print("  Binance Liquidation + Volume Bot")
    print("=" * 50)

    # Перевіряємо чи налаштований TG
    if not TG_TOKEN or not TG_CHAT_ID:
        print("\n❌ ПОМИЛКА: Задай змінні TG_TOKEN та TG_CHAT_ID в Railway!\n")
        return

    await liquidation_stream()


if __name__ == "__main__":
    asyncio.run(main())
