import os, time, pyotp, requests, datetime, logging, json, urllib.request, threading, queue
from flask import Flask
# SmartConnect will be imported lazily when login is required
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(filename='trushu_history.log', level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- FLASK SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Trushu Bot is Live!"
def run_server(): app.run(host='0.0.0.0', port=8080)

def pro_log(msg, level=logging.INFO):
    print(msg, flush=True)
    logging.log(level, msg.replace('\n', ' '))

def pro_exception(msg, exc):
    print(f"{msg}: {exc}", flush=True)
    logging.exception("%s: %s", msg.replace('\n', ' '), exc)

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_VIP_CHAT_ID = "-1004373250401"
TELEGRAM_FREE_CHAT_ID = "-1003688562093"
VIP_JOIN_LINK = "https://t.me/+fYBPh_cNLtkzZjNl"

INSTRUMENTS = [
    {"name": "BANKNIFTY", "exchange": "NSE", "symbol": "Nifty Bank", "token": "26009", "yahoo": "^NSEBANK", "step": 20},
    {"name": "NIFTY", "exchange": "NSE", "symbol": "Nifty 50", "token": "26000", "yahoo": "^NSEI", "step": 10}
]

active_trades = {"BANKNIFTY": None, "NIFTY": None}
cooldowns = {"BANKNIFTY": 0, "NIFTY": 0}
daily_sl_hits = {"BANKNIFTY": 0, "NIFTY": 0}
free_signals_sent = 0
morning_msg_sent = False
SMARTAPI_SESSION_TTL_SECONDS = 45 * 60
SMARTAPI_LOGIN_TIMEOUT_SECONDS = 20
SMARTAPI_LOGIN_RETRY_SECONDS = 60
PCR_CACHE_TTL_SECONDS = 5 * 60
pcr_cache = {"timestamp": 0, "data": {}}

# =========================
# TRUSHU ANALYSIS CONFIG
# =========================
SAFE_MODE = True

RSI_BUY_LEVEL = 55
RSI_SELL_LEVEL = 45

PCR_BUY_MIN = 0.80
PCR_SELL_MAX = 1.20

MAX_SL_PER_DAY = 2
MAX_FREE_SIGNALS_PER_DAY = 1

COOLDOWN_SECONDS = 15 * 60

USE_ANGEL_HISTORICAL = True
ALLOW_HISTORICAL_FALLBACK = True

LOG_SIGNAL_DETAILS = True
# =========================



class SmartAPISessionExpired(Exception):
    pass


class SmartAPISessionManager:
    def __init__(self):
        self.api = None
        self.login_time = 0
        self.last_login_attempt = 0
        self.login_thread = None
        self.login_result_queue = None
        self.lock = threading.Lock()

    def get_session(self, force=False):
        with self.lock:
            needs_login = force or self._needs_login()
            if not needs_login:
                return self.api
            if self.login_thread and self.login_thread.is_alive():
                pro_log("[SMARTAPI] Previous login attempt still running; not starting another worker.", logging.WARNING)
                return None
            if not force and self.last_login_attempt and time.time() - self.last_login_attempt < SMARTAPI_LOGIN_RETRY_SECONDS:
                remaining = int(SMARTAPI_LOGIN_RETRY_SECONDS - (time.time() - self.last_login_attempt))
                pro_log(f"[SMARTAPI] Login retry cooling down for {remaining}s.", logging.WARNING)
                return None
            self.last_login_attempt = time.time()

        api, login_time = self._login_with_timeout()

        with self.lock:
            self.api = api
            self.login_time = login_time if api else 0
            return self.api

    def invalidate(self, reason):
        with self.lock:
            self.api = None
            self.login_time = 0
            self.last_login_attempt = 0
        pro_log(f"[SMARTAPI] Session invalidated: {reason}", logging.WARNING)

    def _needs_login(self):
        if self.api is None:
            return True
        age = time.time() - self.login_time
        if age >= SMARTAPI_SESSION_TTL_SECONDS:
            pro_log("[SMARTAPI] Cached session reached 45 minutes. Re-login required.")
            return True
        return False

    def _login_worker(self, result_queue):
        try:
            from SmartApi import SmartConnect
            smart_api = SmartConnect(api_key=API_KEY)
            data = smart_api.generateSession(CLIENT_ID, PASSWORD, pyotp.TOTP(TOTP_SECRET).now())
            result_queue.put((smart_api, data, None))
        except Exception as e:
            result_queue.put((None, None, e))

    def _finish_login_attempt(self):
        try:
            smart_api, data, error = self.login_result_queue.get_nowait()
        except queue.Empty:
            pro_log("[SMARTAPI] Login ended without a response. Retry later.", logging.ERROR)
            return None, 0
        finally:
            self.login_thread = None
            self.login_result_queue = None

        if error:
            pro_exception("[SMARTAPI] Login exception", error)
            return None, 0

        if data and data.get('status'):
            pro_log("[SMARTAPI] Login successful. Session cached.")
            return smart_api, time.time()

        message = data.get('message') if isinstance(data, dict) else data
        pro_log(f"[SMARTAPI] Login failed: {message}", logging.ERROR)
        return None, 0

    def _login_with_timeout(self):
        if not all([API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET]):
            pro_log("[SMARTAPI] Missing Angel One credentials in environment.", logging.ERROR)
            return None, 0

        with self.lock:
            if self.login_thread and self.login_thread.is_alive():
                pro_log("[SMARTAPI] Previous login attempt still running; not starting another worker.", logging.WARNING)
                return None, 0
            if self.login_thread and self.login_result_queue:
                return self._finish_login_attempt()

            pro_log(f"[SMARTAPI] Logging in to Angel One with {SMARTAPI_LOGIN_TIMEOUT_SECONDS}s timeout...")
            self.login_result_queue = queue.Queue(maxsize=1)
            self.login_thread = threading.Thread(target=self._login_worker, args=(self.login_result_queue,), daemon=True)
            self.login_thread.start()
            login_thread = self.login_thread

        login_thread.join(SMARTAPI_LOGIN_TIMEOUT_SECONDS)

        with self.lock:
            if login_thread.is_alive():
                pro_log("[SMARTAPI] Login timed out. Main loop will continue and retry later.", logging.ERROR)
                return None, 0
            return self._finish_login_attempt()

smartapi_session = SmartAPISessionManager()


def is_smartapi_session_error(response):
    if not isinstance(response, dict):
        return False
    text = " ".join(str(response.get(k, "")) for k in ("message", "errorcode", "error", "data")).lower()
    return any(term in text for term in ("invalid token", "token expired", "session expired", "jwt", "unauthorized"))


def load_scrip_master():
    file_name = "scrip_master.json"
    if not os.path.exists(file_name) or (time.time() - os.path.getmtime(file_name)) > 86400:
        try:
            pro_log("[INFO] Downloading Angel One Options Data...")
            urllib.request.urlretrieve("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", file_name)
            pro_log("[SUCCESS] Options Data Downloaded!")
        except Exception as e:
            pro_exception("[ERROR] Options data download failed", e)
    try:
        with open(file_name, "r") as f: return json.load(f)
    except Exception as e:
        pro_exception("[ERROR] Unable to load scrip master", e)
        return []

scrip_data = None

def get_scrip_data():
    global scrip_data
    if scrip_data is None:
        scrip_data = load_scrip_master()
    return scrip_data

def strike_matches(item_strike, target_strike):
    try:
        item_value = float(item_strike)
        target_value = float(target_strike)
    except (TypeError, ValueError):
        return False
    return abs(item_value - target_value) < 0.01 or abs((item_value / 100) - target_value) < 0.01

def get_option_token(index_name, strike, opt_type):
    index = "NIFTY" if index_name == "NIFTY" else "BANKNIFTY"
    today = datetime.datetime.today().date()
    valid_options = []
    for item in get_scrip_data():
        if item['name'] == index and item['instrumenttype'] == 'OPTIDX' and strike_matches(item.get('strike'), strike) and item['symbol'].endswith(opt_type):
            try:
                exp_date = datetime.datetime.strptime(item['expiry'], '%d%b%Y').date()
                if exp_date >= today: valid_options.append((exp_date, item['token'], item['symbol']))
            except Exception as e:
                pro_log(f"[WARN] Skipping invalid expiry for {item.get('symbol')}: {e}", logging.WARNING)
    if valid_options:
        valid_options.sort(key=lambda x: x[0])
        token, symbol = valid_options[0][1], valid_options[0][2]
        pro_log(f"[DEBUG] {index_name} | ATM Strike:{strike} | CE/PE:{opt_type} | Selected option token:{token} | Selected option symbol:{symbol}")
        return token, symbol
    pro_log(f"[DEBUG] {index_name} | ATM Strike:{strike} | CE/PE:{opt_type} | No option token found", logging.WARNING)
    return None, None

def send_telegram_message(message, chat_id):
    try:
        response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": message}, timeout=10)
        if not response.ok:
            pro_log(f"[TELEGRAM] sendMessage failed for {chat_id}: {response.status_code} {response.text[:200]}", logging.WARNING)
    except Exception as e:
        pro_exception(f"[TELEGRAM] sendMessage exception for {chat_id}", e)

def send_telegram_photo(photo_path, caption, chat_id):
    try:
        with open(photo_path, 'rb') as photo:
            response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data={"chat_id": chat_id, "caption": caption}, files={"photo": photo}, timeout=20)
            if not response.ok:
                pro_log(f"[TELEGRAM] sendPhoto failed for {chat_id}: {response.status_code} {response.text[:200]}", logging.WARNING)
                send_telegram_message(caption, chat_id)
    except Exception as e:
        pro_exception(f"[TELEGRAM] sendPhoto exception for {chat_id}", e)
        send_telegram_message(caption, chat_id)

def login_angel_one():
    return smartapi_session.get_session()

def get_live_price(api, exchange, symbol, token):
    try:
        response = api.ltpData(exchange, symbol, token)
        if response and response.get('status'):
            return response['data']['ltp']
        if is_smartapi_session_error(response):
            raise SmartAPISessionExpired(response.get('message', 'SmartAPI session expired'))
        pro_log(f"[SMARTAPI] LTP failed for {exchange}:{symbol} ({token}): {response}", logging.WARNING)
        return None
    except SmartAPISessionExpired:
        raise
    except Exception as e:
        pro_exception(f"[SMARTAPI] LTP exception for {exchange}:{symbol} ({token})", e)
        return None

def extract_pcr_value(item):
    if not isinstance(item, dict):
        return None
    for key in ("pcr", "putCallRatio", "put_call_ratio", "putCallRatioValue"):
        if key in item:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                return None
    put_value = call_value = None
    for key in ("putOI", "putOi", "put_oi", "put"):
        if key in item:
            try:
                put_value = float(item[key])
                break
            except (TypeError, ValueError):
                pass
    for key in ("callOI", "callOi", "call_oi", "call"):
        if key in item:
            try:
                call_value = float(item[key])
                break
            except (TypeError, ValueError):
                pass
    if put_value is not None and call_value:
        return put_value / call_value
    return None

def normalize_pcr_name(value):
    return str(value or "").upper().replace(" ", "").replace("-", "")

def parse_pcr_response(response):
    if not response or not isinstance(response, dict):
        return {}
    data = response.get("data")
    if isinstance(data, dict):
        rows = data.get("data") or data.get("records") or data.get("values") or data.get("pcr") or data
    else:
        rows = data
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return {}

    parsed = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = normalize_pcr_name(item.get("name") or item.get("symbol") or item.get("index") or item.get("tradingSymbol"))
        pcr = extract_pcr_value(item)
        if pcr is None:
            continue
        if "BANKNIFTY" in name or "NIFTYBANK" in name or name == "BANK":
            parsed["BANKNIFTY"] = pcr
        elif "NIFTY" in name:
            parsed["NIFTY"] = pcr
    return parsed

def get_pcr(api, name):
    now = time.time()
    if now - pcr_cache["timestamp"] < PCR_CACHE_TTL_SECONDS:
        return pcr_cache["data"].get(name)
    try:
        response = api.putCallRatio()
        if response and response.get("status"):
            pcr_cache["data"] = parse_pcr_response(response)
            pcr_cache["timestamp"] = now
            pro_log(f"[PCR] Cache refreshed: {pcr_cache['data']}")
            return pcr_cache["data"].get(name)
        if is_smartapi_session_error(response):
            raise SmartAPISessionExpired(response.get('message', 'SmartAPI session expired'))
        pro_log(f"[PCR] putCallRatio unavailable: {response}", logging.WARNING)
    except SmartAPISessionExpired:
        raise
    except Exception as e:
        pro_exception("[PCR] putCallRatio exception", e)
    pcr_cache["timestamp"] = now
    pcr_cache["data"] = {}
    return None

def get_candles(ticker, interval):
    try:
        response = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval={interval}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        response.raise_for_status()
        return [c for c in response.json()['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
    except Exception as e:
        pro_exception(f"[YAHOO] Candle fetch failed for {ticker} {interval}", e)
        return None

def calculate_ema(prices, period):
    if len(prices) < period: return [0] * len(prices)
    m = 2 / (period + 1); emas = [None] * (period - 1) + [sum(prices[:period]) / period]
    for p in prices[period:]: emas.append((p - emas[-1]) * m + emas[-1])
    return emas

def calculate_rsi(prices, period=14):
    if len(prices) <= period: return [50] * len(prices)
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gains, losses = [d if d > 0 else 0 for d in deltas], [abs(d) if d < 0 else 0 for d in deltas]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    rsis = [None] * period
    for i in range(period, len(prices)-1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period; avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsis.append(100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss != 0 else 100)
    return rsis

def get_atm_strike(name, price): return int(round(price / 100) * 100) if name == "BANKNIFTY" else int(round(price / 50) * 50)

def format_price(value):
    return f"₹{float(value):.2f}"

def format_time():
    return datetime.datetime.now().strftime("%H:%M")

def calculate_confidence(opt_type, rsi, pcr):
    if not opt_type:
        return 0
    confidence = 70
    if opt_type == "CE" and rsi >= 60:
        confidence += 10
    elif opt_type == "PE" and rsi <= 40:
        confidence += 10
    if pcr is not None:
        if opt_type == "CE" and pcr >= 0.80:
            confidence += 10
        elif opt_type == "PE" and pcr <= 1.20:
            confidence += 10
    return max(1, min(confidence, 99))

def build_buy_message(trade):
    return (
        f"🟢 BUY {trade['symbol']}\n\n"
        f"💰 Entry : {format_price(trade['entry'])}\n"
        f"🛑 Stop Loss : {format_price(trade['stoploss'])}\n\n"
        f"🎯 Target 1 : {format_price(trade['target1'])}\n"
        f"🎯 Target 2 : {format_price(trade['target2'])}\n\n"
        f"⏰ Time : {format_time()}\n\n"
        f"📊 Confidence : {trade['confidence']}%\n\n"
        "🔥 TRUSHU ANALYSIS VIP"
    )

def build_update_message(trade, premium):
    profit = float(premium) - float(trade['entry'])
    return (
        "📈 UPDATE\n\n"
        f"{trade['symbol']}\n\n"
        f"Current Premium : {format_price(premium)}\n\n"
        f"Profit : {format_price(profit)}\n\n"
        "New High ✅"
    )

def build_status_message(title, trade, premium):
    profit = float(premium) - float(trade['entry'])
    return f"{title}\n\n{trade['symbol']}\n\nCurrent Premium : {format_price(premium)}\nProfit : {format_price(profit)}"

def has_open_trade():
    return any(trade and trade.get('status') == 'OPEN' for trade in active_trades.values())

def reset_trade(name, trade, is_sl=False):
    if is_sl:
        daily_sl_hits[name] += 1
    cooldowns[name] = time.time() + COOLDOWN_SECONDS
    active_trades[name] = None

def check_signal(api, name, live_price, yahoo_ticker):
    c_5m, c_15m = get_candles(yahoo_ticker, "5m"), get_candles(yahoo_ticker, "15m")
    if not c_5m or not c_15m:
        if SAFE_MODE:
            pro_log(f"[SAFE MODE] {name} | Historical candles unavailable. Skipping signal.", logging.WARNING)
        return None
    ema9_5m, ema21_5m = calculate_ema(c_5m, 9)[-1], calculate_ema(c_5m, 21)[-1]
    rsi14_5m = calculate_rsi(c_5m, 14)[-1]
    ema9_15m, ema21_15m = calculate_ema(c_15m, 9)[-1], calculate_ema(c_15m, 21)[-1]
    trend_15m = "UP" if ema9_15m > ema21_15m else "DOWN"
    
    # RSI Condition loosened from 60/40 to 55/45 for more signals
    opt_type = "CE" if (ema9_5m > ema21_5m and rsi14_5m >= RSI_BUY_LEVEL and trend_15m == "UP") else ("PE" if (ema9_5m < ema21_5m and rsi14_5m <= RSI_SELL_LEVEL and trend_15m == "DOWN") else None)
    strategy_signal = opt_type or "NONE"
    pcr = get_pcr(api, name)
    pcr_text = f"{pcr:.2f}" if pcr is not None else "UNAVAILABLE"

    if pcr is not None:
        if opt_type == "CE" and pcr < PCR_BUY_MIN:
            opt_type = None
        elif opt_type == "PE" and pcr > PCR_SELL_MAX:
            opt_type = None

    final_decision = opt_type or "NO_SIGNAL"
    pro_log(f"[DEBUG] {name} | Price:{live_price} | RSI:{rsi14_5m:.2f} | 5m EMA9/21:{ema9_5m:.0f}/{ema21_5m:.0f} | 15m EMA9/21:{ema9_15m:.0f}/{ema21_15m:.0f} | 15m Trend:{trend_15m} | PCR:{pcr_text} | Strategy:{strategy_signal} | Final:{final_decision}")
    
    if opt_type:
        strike = get_atm_strike(name, live_price)
        pro_log(f"[DEBUG] {name} | ATM Strike:{strike} | CE/PE selection:{opt_type}")
        opt_token, opt_symbol = get_option_token(name, strike, opt_type)
        if opt_token:
            p = int(get_live_price(api, "NFO", opt_symbol, opt_token) or 0)
            if p > 0:
                sl, t1, t2 = (p-40, p+60, p+120) if name=="BANKNIFTY" else (p-20, p+40, p+90)
                confidence = calculate_confidence(opt_type, rsi14_5m, pcr)
                return {"type": "BUY", "opt": opt_type, "strike": strike, "entry": float(p), "sl": float(sl), "t1": float(t1), "t2": float(t2), "t1_hit": False, "opt_token": opt_token, "opt_symbol": opt_symbol, "symbol": opt_symbol, "token": opt_token, "stoploss": float(sl), "target1": float(t1), "target2": float(t2), "highest_premium": float(p), "current_premium": float(p), "last_notified": float(p), "last_notified_price": float(p), "status": "OPEN", "confidence": confidence, "target1_notified": False, "target2_notified": False, "sl_notified": False, "is_free": False}
    return None

def manage_trade(name, premium_price, trade, step):
    if not trade or trade.get('status') != 'OPEN':
        return trade

    p = float(premium_price)
    trade['current_premium'] = p

    if p <= trade['stoploss'] and not trade.get('sl_notified'):
        send_telegram_message(build_status_message("🔴 STOP LOSS HIT", trade, p), TELEGRAM_VIP_CHAT_ID)
        if trade.get('is_free'): send_telegram_message(build_status_message("🔴 STOP LOSS HIT", trade, p), TELEGRAM_FREE_CHAT_ID)
        trade['sl_notified'] = True
        trade['status'] = 'CLOSED'
        reset_trade(name, trade, is_sl=True)
        pro_log(f"[TRADE] {trade['symbol']} stop loss hit at {p:.2f}")
        return None

    if p >= trade['target1'] and not trade.get('target1_notified'):
        send_telegram_message(build_status_message("🎯 TARGET 1 HIT", trade, p), TELEGRAM_VIP_CHAT_ID)
        if trade.get('is_free'): send_telegram_message(build_status_message("🎯 TARGET 1 HIT", trade, p), TELEGRAM_FREE_CHAT_ID)
        trade['target1_notified'] = True
        trade['t1_hit'] = True
        trade['sl'] = trade['entry']
        trade['stoploss'] = trade['entry']
        trade['last_notified'] = p
        trade['last_notified_price'] = p
        pro_log(f"[TRADE] {trade['symbol']} target 1 hit at {p:.2f}")

    if p >= trade['target2'] and not trade.get('target2_notified'):
        send_telegram_message(build_status_message("🏆 TARGET 2 HIT", trade, p), TELEGRAM_VIP_CHAT_ID)
        if trade.get('is_free'): send_telegram_message(build_status_message("🏆 TARGET 2 HIT", trade, p), TELEGRAM_FREE_CHAT_ID)
        trade['target2_notified'] = True
        trade['status'] = 'CLOSED'
        reset_trade(name, trade, is_sl=False)
        pro_log(f"[TRADE] {trade['symbol']} target 2 hit at {p:.2f}")
        return None

    if p > trade['highest_premium']:
        trade['highest_premium'] = p
        if p - trade['last_notified_price'] >= 5:
            send_telegram_message(build_update_message(trade, p), TELEGRAM_VIP_CHAT_ID)
            if trade.get('is_free'): send_telegram_message(build_update_message(trade, p), TELEGRAM_FREE_CHAT_ID)
            trade['last_notified'] = p
            trade['last_notified_price'] = p
            pro_log(f"[TRADE] {trade['symbol']} new high update at {p:.2f}")

    return trade

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    pro_log("🚀 TRUSHU ANALYSIS VIP & FREE FUNNEL STARTED")
    
    while True:
        try:
            now = datetime.datetime.now()
            # 1. Morning Post
            if now.weekday() < 5 and now.hour == 9 and 0 <= now.minute < 15 and not morning_msg_sent:
                caption = "🌅 Good Morning! Market open hone wala hai."
                if os.path.exists("morningpost.png"): send_telegram_photo("morningpost.png", caption, TELEGRAM_VIP_CHAT_ID); send_telegram_photo("morningpost.png", caption, TELEGRAM_FREE_CHAT_ID)
                else: send_telegram_message(caption, TELEGRAM_VIP_CHAT_ID); send_telegram_message(caption, TELEGRAM_FREE_CHAT_ID)
                morning_msg_sent = True
            
            # 2. Trading
            if now.weekday() < 5 and ((now.hour == 9 and now.minute >= 15) or (9 < now.hour < 15) or (now.hour == 15 and now.minute <= 30)):
                pro_log(f"[DEBUG] Main loop active | Time:{now.strftime('%H:%M:%S')} | Checking SmartAPI session")
                api = login_angel_one()
                if api:
                    for inst in INSTRUMENTS:
                        name = inst['name']
                        if daily_sl_hits[name] < MAX_SL_PER_DAY:
                            if active_trades[name]:
                                ltp = get_live_price(api, "NFO", active_trades[name]['opt_symbol'], active_trades[name]['opt_token'])
                                if ltp: active_trades[name] = manage_trade(name, ltp, active_trades[name], inst['step'])
                            elif not has_open_trade() and time.time() > cooldowns[name]:
                                idx = get_live_price(api, inst['exchange'], inst['symbol'], inst['token'])
                                if idx:
                                    nt = check_signal(api, name, idx, inst['yahoo'])
                                    if nt:
                                        active_trades[name] = nt
                                        msg = build_buy_message(nt)
                                        send_telegram_message(msg, TELEGRAM_VIP_CHAT_ID)
                                        if free_signals_sent < 1:
                                            nt['is_free'] = True; send_telegram_message(f"🎁 **FREE** 🎁\n{msg}\n👉 Join VIP: {VIP_JOIN_LINK}", TELEGRAM_FREE_CHAT_ID); free_signals_sent += 1
                                        pro_log(f"[TRADE] Opened {nt['symbol']} | Token:{nt['token']} | Entry:{nt['entry']:.2f} | SL:{nt['stoploss']:.2f} | T1:{nt['target1']:.2f} | T2:{nt['target2']:.2f}")
            elif now.hour >= 16 or now.hour < 9:
                active_trades, daily_sl_hits, free_signals_sent, morning_msg_sent = {"BANKNIFTY": None, "NIFTY": None}, {"BANKNIFTY": 0, "NIFTY": 0}, 0, False
            time.sleep(60)
        except SmartAPISessionExpired as e:
            smartapi_session.invalidate(e)
            pro_log(f"[SMARTAPI] Session expired. Re-login will be attempted on next cycle: {e}", logging.WARNING)
            time.sleep(5)
        except Exception as e:
            pro_exception("[ERROR] Main loop exception", e)
            time.sleep(10)
