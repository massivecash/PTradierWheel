
import time
import datetime as dt
import pytz
import requests
import sys, io


from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Access them
API_KEY = os.getenv('PTRADIER_API_KEY')
API_SECRET = os.getenv('PTRADIER_API_SECRET')
API_BASE_URL = os.getenv('PTRADIER_BASE_URL')

//print(f"Loaded: {API_KEY[:10]}...")


# Force UTF-8 (fix Windows crash)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ===================== CONFIG =====================
// API_KEY = "EbMPd1iAPHs7pVHowGus49zHUQ2h"
// ACCOUNT_ID = "VA81120776"
// BASE_URL = "https://sandbox.tradier.com/v1"  # Paper trading

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json"
}

SYMBOL = "SPY"
TIMEZONE = pytz.timezone("US/Eastern")

MIN_IV = 0.25
BASE_DELTA = 0.25
LOW_VOL_DELTA = 0.30
HIGH_VOL_DELTA = 0.15

PROFIT_TAKE = 0.50
STOP_LOSS_MULTIPLIER = 2.0
DELTA_EXIT = 0.60
DTE_EXIT = 21  # Hard exit

MAX_VIX = 28
KILL_SWITCH_DD = -0.10

EVENT_DATES = ["2026-04-10", "2026-04-17"]

LOG_FILE = "wheel.log"

# ===================== STATE =====================
state = {
    "active_option": None,
    "entry_price": None,
    "equity_start": None
}

# ===================== LOGGER =====================
def log(msg):
    ts = dt.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ===================== UTILS =====================
def req(method, url, **kwargs):
    try:
        return requests.request(method, url, headers=HEADERS, **kwargs).json()
    except Exception as e:
        log(f"ERROR_REQ: {e}")
        return None

# ===================== MARKET =====================
def get_vix():
    r = req("GET", f"{BASE_URL}/markets/quotes", params={"symbols": "VIX"})
    try:
        return float(r["quotes"]["quote"]["last"])
    except:
        return 999

def is_event_day():
    today = dt.date.today().strftime("%Y-%m-%d")
    return today in EVENT_DATES

def is_market_open():
    r = req("GET", f"{BASE_URL}/markets/clock")
    return r and r["clock"]["state"] == "open"

def get_price(symbol):
    r = req("GET", f"{BASE_URL}/markets/quotes", params={"symbols": symbol})
    q = r["quotes"]["quote"]
    return (q["ask"] + q["bid"]) / 2

def market_regime():
    """
    Simple proxy:
    - Compressed price → RANGE
    - Deviating → TREND
    """
    price = get_price(SYMBOL)
    if abs(price % 5) < 1:
        log(f"Market regime: RANGE (Price={price:.2f})")
        return "RANGE"
    log(f"Market regime: TREND (Price={price:.2f})")
    return "TREND"

def dynamic_delta(vix):
    if vix < 18:
        return LOW_VOL_DELTA
    elif vix > 25:
        return HIGH_VOL_DELTA
    return BASE_DELTA

# ===================== ACCOUNT =====================
def get_equity():
    r = req("GET", f"{BASE_URL}/accounts/{ACCOUNT_ID}/balances")
    try:
        return float(r["balances"]["total_equity"])
    except:
        return None

def kill_switch():
    if not state["equity_start"]:
        state["equity_start"] = get_equity()
        return False
    dd = (get_equity() - state["equity_start"]) / state["equity_start"]
    if dd <= KILL_SWITCH_DD:
        log("KILL SWITCH TRIGGERED")
        close_all_positions()
        return True
    return False

# ===================== OPTIONS =====================
def get_option_chain():
    exp = (dt.date.today() + dt.timedelta(days=30)).strftime("%Y-%m-%d")
    r = req("GET", f"{BASE_URL}/markets/options/chains", params={
        "symbol": SYMBOL,
        "expiration": exp,
        "greeks": "true"
    })
    return r.get("options", {}).get("option", []) if r else []

def pick_option(chain, type_, target_delta):
    for opt in chain:
        if opt["option_type"] != type_:
            continue
        greeks = opt.get("greeks", {})
        delta = abs(greeks.get("delta", 0))
        iv = greeks.get("mid_iv")
        if iv and iv >= MIN_IV and abs(delta - target_delta) < 0.05:
            return opt
    return None

# ===================== EXECUTION =====================
def place(symbol, side, price):
    log(f"Placing order {side} {symbol} @ {price}")
    req("POST", f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders", data={
        "class": "option",
        "symbol": symbol,
        "side": side,
        "quantity": 1,
        "type": "limit",
        "price": price,
        "duration": "day"
    })

def close_all_positions():
    log("CLOSE ALL POSITIONS (manual implementation recommended)")

# ===================== POSITION MANAGEMENT =====================
def manage_position(chain):
    if not state["active_option"]:
        return
    for opt in chain:
        if opt["symbol"] == state["active_option"]:
            price = opt["last"]
            delta = abs(opt.get("greeks", {}).get("delta", 0))
            exp = dt.datetime.strptime(opt["expiration_date"], "%Y-%m-%d").date()
            dte = (exp - dt.date.today()).days

            if dte < DTE_EXIT:
                log("TIME DTE EXIT")
                place(opt["symbol"], "buy_to_close", price)
                state["active_option"] = None
                return
            if price >= state["entry_price"] * STOP_LOSS_MULTIPLIER:
                log("PRICE STOP LOSS")
                place(opt["symbol"], "buy_to_close", price)
                state["active_option"] = None
                return
            if delta >= DELTA_EXIT:
                log("PRICE DELTA EXIT")
                place(opt["symbol"], "buy_to_close", price)
                state["active_option"] = None
                return
            if price <= state["entry_price"] * (1 - PROFIT_TAKE):
                log("TAKE PROFIT")
                place(opt["symbol"], "buy_to_close", price)
                state["active_option"] = None
                return

# ===================== TAIL HEDGE =====================
def tail_hedge(chain):
    for opt in chain:
        delta = abs(opt.get("greeks", {}).get("delta", 0))
        if opt["option_type"] == "put" and delta < 0.05:
            log(f"Buying hedge {opt['symbol']}")
            place(opt["symbol"], "buy_to_open", opt["last"])
            return

# ===================== MAIN LOOP =====================
def run():
    while True:
        try:
            if not is_market_open():
                log("Market not open")
                time.sleep(300)
                continue

            if is_event_day():
                log("Event Day - no trades")
                time.sleep(3600)
                continue

            if kill_switch():
                break

            vix = get_vix()
            if vix > MAX_VIX:
                log(f"High VIX {vix}")
                tail_hedge(get_option_chain())
                time.sleep(600)
                continue

            regime = market_regime()
            delta = dynamic_delta(vix)
            chain = get_option_chain()

            if not state["active_option"]:
                if regime == "RANGE":
                    opt = pick_option(chain, "put", delta)
                    if opt:
                        log(f"Enter Trade: SELL PUT {opt['symbol']} | regime={regime} | delta={delta}")
                        place(opt["symbol"], "sell_to_open", opt["last"])
                        state["active_option"] = opt["symbol"]
                        state["entry_price"] = opt["last"]
                else:
                    log("Trend regime - no trade")

            manage_position(chain)

            time.sleep(300)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    log("Wheel Strategy BOT STARTED")
    run()
