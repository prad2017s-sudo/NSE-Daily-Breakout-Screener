import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from io import StringIO

IST = ZoneInfo("Asia/Kolkata")

# =========================================================
# APP CONFIG
# =========================================================
st.set_page_config(
    page_title="Nifty 500 HA Bollinger Alert",
    page_icon="ðŸ“Š",
    layout="wide",
)

# =========================================================
# SETTINGS
# =========================================================
BB_LENGTH = 20
BB_MULTIPLIER = 2.0
WATCH_DAYS = 2
WATCH_FILE = "watchlist.json"

NIFTY500_CSV_URL = (
    "https://www.niftyindices.com/"
    "IndexConstituent/ind_nifty500list.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.niftyindices.com/",
    "Connection": "keep-alive",
}

# =========================================================
# CSS
# =========================================================
st.markdown(
    """
<style>
.main-title {
    font-size: 28px;
    font-weight: 800;
    margin-bottom: 2px;
}
.sub-title {
    color: #777;
    font-size: 14px;
    margin-bottom: 14px;
}
.small-note {
    color: #777;
    font-size: 12px;
}
</style>
""",
    unsafe_allow_html=True,
)

# =========================================================
# SESSION STATE
# =========================================================
if "scanner_on" not in st.session_state:
    st.session_state.scanner_on = True
if "last_scan_date" not in st.session_state:
    st.session_state.last_scan_date = None
if "last_scan_time" not in st.session_state:
    st.session_state.last_scan_time = None
if "universe_source" not in st.session_state:
    st.session_state.universe_source = ""

# =========================================================
# WATCHLIST STORAGE
# =========================================================
def load_watchlist():
    if not os.path.exists(WATCH_FILE):
        return []
    try:
        with open(WATCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_watchlist(data):
    try:
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# =========================================================
# NIFTY 500 UNIVERSE
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_symbols():
    response = requests.get(
        NIFTY500_CSV_URL,
        timeout=30,
        headers=HEADERS,
    )
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))

    symbol_col = next(
        (
            col for col in df.columns
            if str(col).strip().lower().replace(" ", "") == "symbol"
        ),
        None,
    )
    if symbol_col is None:
        raise ValueError("Official CSV mein Symbol column nahi mila.")

    symbols = (
        df[symbol_col]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .drop_duplicates()
        .tolist()
    )
    if len(symbols) < 450:
        raise ValueError(
            f"Nifty 500 list incomplete hai: {len(symbols)} symbols mile."
        )
    return symbols


# =========================================================
# YAHOO FINANCE DAILY DATA
# =========================================================
def yf_symbol(symbol):
    return f"{symbol}.NS"


@st.cache_data(ttl=1800, show_spinner=False)
def download_daily(symbol):
    try:
        df = yf.Ticker(yf_symbol(symbol)).history(
            period="15mo",
            interval="1d",
            auto_adjust=False,
            actions=False,
        )
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        required = ["Open", "High", "Low", "Close"]
        for col in required:
            if col not in df.columns:
                return pd.DataFrame()
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=required)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    except Exception:
        return pd.DataFrame()


# =========================================================
# HEIKIN ASHI
# =========================================================
def heikin_ashi(df):
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame(index=df.index)
    o = df["Open"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)

    ha_close = (o + h + l + c) / 4.0
    ha_open = np.zeros(len(df), dtype=float)
    ha_open[0] = (o.iloc[0] + c.iloc[0]) / 2.0

    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_open_series = pd.Series(ha_open, index=df.index)
    out["HA_Open"] = ha_open_series
    out["HA_High"] = pd.concat(
        [ha_open_series, ha_close, h], axis=1
    ).max(axis=1)
    out["HA_Low"] = pd.concat(
        [ha_open_series, ha_close, l], axis=1
    ).min(axis=1)
    out["HA_Close"] = ha_close
    return out


# =========================================================
# BOLLINGER BANDS ON HEIKIN ASHI CLOSE
# =========================================================
def add_bollinger(ha):
    if ha.empty:
        return ha

    out = ha.copy()
    out["Middle"] = out["HA_Close"].rolling(BB_LENGTH).mean()
    out["Std"] = out["HA_Close"].rolling(BB_LENGTH).std(ddof=0)
    out["Upper"] = out["Middle"] + BB_MULTIPLIER * out["Std"]
    out["Lower"] = out["Middle"] - BB_MULTIPLIER * out["Std"]
    return out


# =========================================================
# SIGNAL RULE
# Candle 1: NORMAL candle Low < its Lower Bollinger Band
# Candle 2: NORMAL candle Low > its Lower Bollinger Band
#           AND candle 2 High <= candle 1 High
# Watch the setup for up to 2 trading sessions after signal.
# BUY trigger is candle 1's High, checked on later candles.
# =========================================================
def find_cross_signal(df):
    if df.empty or len(df) < BB_LENGTH + 2:
        return None

    ha = heikin_ashi(df)
    bb = add_bollinger(ha)

    # Use the latest two daily bars. Data provider may include
    # today's still-forming bar during market hours.
    prev = bb.iloc[-2]
    curr = bb.iloc[-1]
    prev_normal = df.iloc[-2]
    curr_normal = df.iloc[-1]

    if pd.isna(prev["Lower"]) or pd.isna(curr["Lower"]):
        return None

    first_condition = float(prev_normal["Low"]) < float(prev["Lower"])
    second_condition = float(curr_normal["Low"]) > float(curr["Lower"])
    no_high_break = float(curr_normal["High"]) <= float(prev_normal["High"])

    if not (first_condition and second_condition and no_high_break):
        return None

    signal_date = pd.Timestamp(bb.index[-1]).date()
    return {
        "signal_date": str(signal_date),
        "trigger_high": float(prev_normal["High"]),
        "first_candle_low": float(prev_normal["Low"]),
        "second_candle_low": float(curr_normal["Low"]),
        "ha_close": float(curr["HA_Close"]),
        "middle": float(curr["Middle"]),
        "lower": float(curr["Lower"]),
    }


# =========================================================
# TRADING-DAY HELPERS
# =========================================================
def trading_days_after(signal_date, days=2):
    try:
        start = pd.Timestamp(signal_date)
        end = start + pd.Timedelta(days=14)
        weekdays = pd.date_range(
            start=start + pd.Timedelta(days=1),
            end=end,
            freq="B",
        )
        return [str(x.date()) for x in weekdays[:days]]
    except Exception:
        return []


def days_left(signal_date):
    dates = trading_days_after(signal_date, WATCH_DAYS)
    today = datetime.now(IST).date()
    return sum(
        1 for d in dates
        if pd.Timestamp(d).date() >= today
    )


# =========================================================
# CHECK ENTRY / TRIGGER
# =========================================================
def check_entry(symbol, trigger_high, start_date):
    df = download_daily(symbol)
    if df.empty:
        return None

    data = df.copy()
    data.index = pd.to_datetime(data.index)

    try:
        data = data[data.index.date > start_date]
    except Exception:
        return None

    if data.empty:
        return None

    data = data.head(WATCH_DAYS)
    for idx, row in data.iterrows():
        if float(row["High"]) > float(trigger_high):
            return {
                "entry_date": str(pd.Timestamp(idx).date()),
                "entry_time": "Daily candle; exact intraday time unavailable",
                "entry_price": float(trigger_high),
            }
    return None


# =========================================================
# UPDATE WATCHLIST
# =========================================================
def update_watchlist(watchlist):
    changed = False
    active = []
    today = datetime.now(IST).date()

    for item in watchlist:
        symbol = item.get("Symbol", "")
        signal_date_text = item.get("Signal Date", "")
        trigger = item.get("Trigger High")
        status = item.get("Status", "WATCHING")

        if not symbol:
            continue

        if status in ("TRIGGERED", "EXPIRED"):
            active.append(item)
            continue

        try:
            signal_date = pd.Timestamp(signal_date_text).date()
            trigger = float(trigger)
        except Exception:
            active.append(item)
            continue

        watch_dates = trading_days_after(signal_date, WATCH_DAYS)
        if not watch_dates:
            active.append(item)
            continue

        watch_dates_dt = [pd.Timestamp(x).date() for x in watch_dates]
        if today not in watch_dates_dt:
            if today > watch_dates_dt[-1]:
                item["Status"] = "EXPIRED"
                item["Days Left"] = 0
                changed = True
            else:
                item["Days Left"] = days_left(signal_date)
            active.append(item)
            continue

        entry = check_entry(symbol, trigger, signal_date)
        if entry:
            item["Status"] = "TRIGGERED"
            item["Entry Date"] = entry["entry_date"]
            item["Entry Time"] = entry["entry_time"]
            item["Entry Price"] = entry["entry_price"]
            item["Days Left"] = 0
            changed = True
        else:
            left = days_left(signal_date)
            if item.get("Days Left") != left:
                item["Days Left"] = left
                changed = True

        active.append(item)

    return active, changed


# =========================================================
# SCAN ONE SYMBOL
# =========================================================
def scan_symbol(symbol):
    df = download_daily(symbol)
    if df.empty:
        return None

    signal = find_cross_signal(df)
    if signal is None:
        return None

    return {
        "Symbol": symbol,
        "Signal Date": signal["signal_date"],
        "Trigger High": signal["trigger_high"],
        "First Candle Low": signal["first_candle_low"],
        "Second Candle Low": signal["second_candle_low"],
        "HA Close": signal["ha_close"],
        "Middle": signal["middle"],
        "Lower Band": signal["lower"],
        "Status": "WATCHING",
        "Entry Date": "",
        "Entry Time": "",
        "Entry Price": "",
        "Days Left": WATCH_DAYS,
    }


def add_new_signals(watchlist, symbols):
    existing_keys = {
        (item.get("Symbol"), item.get("Signal Date"))
        for item in watchlist
    }
    added = 0
    progress = st.progress(0)
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        signal = scan_symbol(symbol)
        if signal:
            key = (signal["Symbol"], signal["Signal Date"])
            if key not in existing_keys:
                watchlist.append(signal)
                existing_keys.add(key)
                added += 1
        progress.progress((i + 1) / max(total, 1))

    progress.empty()
    return watchlist, added


# =========================================================
# MARKET STATUS (DISPLAY ONLY)
# =========================================================
def is_market_time():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    current = now.time()
    start = datetime.strptime("09:00", "%H:%M").time()
    end = datetime.strptime("15:30", "%H:%M").time()
    return start <= current <= end


# =========================================================
# HEADER
# =========================================================
st.markdown(
    '<div class="main-title">ðŸ“Š Nifty 500 Heikin Ashi Bollinger Alert</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="sub-title">'
    'Nifty 500 â€¢ Daily candles â€¢ Heikin Ashi Bollinger 20 / 2 â€¢ '
    'Two-candle setup â€¢ 2 trading-day trigger watch'
    '</div>',
    unsafe_allow_html=True,
)

# =========================================================
# CONTROLS
# =========================================================
c1, c2, c3, c4 = st.columns(4)

with c1:
    button_label = "ðŸŸ¢ Scanner ON" if st.session_state.scanner_on else "ðŸ”´ Scanner OFF"
    if st.button(button_label, use_container_width=True):
        st.session_state.scanner_on = not st.session_state.scanner_on
        st.rerun()

with c2:
    st.metric("Universe", "Nifty 500")
with c3:
    st.metric("Bollinger", "20 / 2")
with c4:
    st.metric("Watch", f"{WATCH_DAYS} Days")

st.caption(
    "Scanner ON hone par app rerun/refresh hone par scan/update karta hai. "
    "Streamlit app apne aap background mein continuously scan nahi karta."
)

# =========================================================
# LOAD UNIVERSE
# =========================================================
try:
    symbols = get_nifty500_symbols()
    st.session_state.universe_source = f"Nifty Indices CSV ({len(symbols)} stocks)"
except Exception as e:
    symbols = []
    st.session_state.universe_source = "CSV unavailable"
    st.error(f"Nifty 500 list load nahi hui: {e}")

st.write(f"ðŸ“Œ **Nifty 500 symbols loaded: {len(symbols)}**")
st.caption(f"Universe source: {st.session_state.universe_source}")

if len(symbols) >= 500:
    st.success(f"âœ… {len(symbols)} symbols loaded.")
elif symbols:
    st.warning(f"Sirf {len(symbols)} symbols mile; official list incomplete ho sakti hai.")

st.info(
    "Setup rule: pehli normal daily candle ka Low apne Lower Band ke neeche ho. "
    "Agli candle ka Low apne Lower Band se upar rahe aur uska High pehli candle "
    "ke High se zyada na ho. Volume condition nahi hai. Watchlist setup ke baad "
    "pehli candle ke High ko agle 2 trading days tak monitor karti hai."
)

# =========================================================
# LOAD AND UPDATE WATCHLIST
# =========================================================
watchlist = load_watchlist()

if st.session_state.scanner_on and symbols:
    watchlist, changed = update_watchlist(watchlist)
    if changed:
        save_watchlist(watchlist)

# =========================================================
# DAILY SCAN
# =========================================================
now = datetime.now(IST)
today = now.date()
market_open_window = is_market_time()

scan_allowed = (
    st.session_state.scanner_on
    and bool(symbols)
    and st.session_state.last_scan_date != today
)

if scan_allowed:
    st.subheader("ðŸ”Ž Daily Nifty 500 Scan")
    with st.spinner(f"Nifty 500 ke {len(symbols)} stocks scan ho rahe hain..."):
        watchlist, added = add_new_signals(watchlist, symbols)

    save_watchlist(watchlist)
    st.session_state.last_scan_date = today
    st.session_state.last_scan_time = now.strftime("%H:%M:%S")

    if added:
        st.success(f"{added} new setup(s) Watchlist mein add hue.")
    else:
        st.info("Is scan mein koi naya setup nahi mila.")

# =========================================================
# MANUAL SCAN
# =========================================================
if st.button("ðŸ”„ Manual Scan Now", use_container_width=True):
    if not symbols:
        st.error("Nifty 500 symbols load nahi hue. Thodi der baad retry karein.")
    else:
        with st.spinner(f"Nifty 500 ke {len(symbols)} stocks scan ho rahe hain..."):
            watchlist, added = add_new_signals(watchlist, symbols)
            watchlist, changed = update_watchlist(watchlist)
            save_watchlist(watchlist)

        st.session_state.last_scan_date = today
        st.session_state.last_scan_time = datetime.now(IST).strftime("%H:%M:%S")

        if added:
            st.success(f"{added} new setup(s) mile.")
        else:
            st.info("Koi naya setup nahi mila.")
        st.rerun()

# =========================================================
# STATUS
# =========================================================
st.markdown("---")
status_col1, status_col2, status_col3 = st.columns(3)

with status_col1:
    if st.session_state.scanner_on:
        st.success("Scanner: ON")
    else:
        st.error("Scanner: OFF")

with status_col2:
    st.write(f"Last scan: {st.session_state.last_scan_time or '--'}")

with status_col3:
    st.write(f"Market window: {'09:00â€“15:30' if market_open_window else 'Closed'}")

# =========================================================
# WATCHLIST DISPLAY
# =========================================================
st.subheader("ðŸ‘€ Watchlist")

if not watchlist:
    st.info("Abhi koi setup Watchlist mein nahi hai.")
else:
    display_columns = [
        "Symbol", "Signal Date", "First Candle Low", "Trigger High",
        "Second Candle Low", "Lower Band", "HA Close", "Middle",
        "Status", "Days Left", "Entry Date", "Entry Time", "Entry Price",
    ]
    rows = [
        {col: item.get(col, "") for col in display_columns}
        for item in watchlist
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )

watch_count = sum(1 for x in watchlist if x.get("Status") == "WATCHING")
triggered_count = sum(1 for x in watchlist if x.get("Status") == "TRIGGERED")
expired_count = sum(1 for x in watchlist if x.get("Status") == "EXPIRED")

a, b, c = st.columns(3)
with a:
    st.metric("Currently Watching", watch_count)
with b:
    st.metric("BUY Trigger Hit", triggered_count)
with c:
    st.metric("Expired", expired_count)

# =========================================================
# UNIVERSE CHECK
# =========================================================
with st.expander("ðŸ” Nifty 500 Universe Check"):
    st.write(f"Total symbols: **{len(symbols)}**")
    st.write(f"Source: **{st.session_state.universe_source}**")
    if symbols:
        universe_df = pd.DataFrame({
            "No.": range(1, len(symbols) + 1),
            "Symbol": symbols,
        })
        st.dataframe(universe_df, use_container_width=True, hide_index=True)

# =========================================================
# RULES
# =========================================================
with st.expander("ðŸ“˜ Scanner Rules"):
    st.markdown(
        f"""
1. Universe: **Nifty 500** (official Nifty Indices CSV).
2. Timeframe: **1 Day**.
3. Indicator: **Heikin Ashi Bollinger Bands**.
4. Bollinger Length: **{BB_LENGTH}**.
5. Standard-deviation multiplier: **{BB_MULTIPLIER}**.
6. First candle's normal Low must be strictly below its Lower Band.
7. Next candle's normal Low must be strictly above its Lower Band.
8. Next candle's High must be less than or equal to the first candle's High.
9. Volume is **not used** in the setup rule.
10. First candle's High is stored as the BUY trigger.
11. Trigger is checked for **{WATCH_DAYS} trading days** after the setup.
12. Trigger hit is determined from daily High data; exact intraday trigger time is unavailable.
13. Data source for OHLC: Yahoo Finance.
14. No broker API or automatic order placement is used.
        """
    )

st.caption(
    "Note: This app scans when Streamlit runs or Manual Scan is clicked. "
    "For unattended scheduled scans, deploy with an external scheduler or a supported "
    "background job. Daily candle values may change while the current session is still open."
)
