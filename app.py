import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NSE Daily Breakout Screener",
    page_icon="ðŸ“ˆ",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Streamlit already supports UTF-8. This CSS improves mobile readability.
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1200px;}
    h1 {font-size: clamp(1.45rem, 4vw, 2.15rem) !important; line-height: 1.35 !important;}
    .stButton button, .stDownloadButton button {width: 100%; min-height: 2.8rem; font-weight: 600;}
    div[data-testid="stMetricValue"] {font-size: clamp(1rem, 3.3vw, 1.7rem);}
    div[data-testid="stAlert"] {line-height: 1.6;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("NSE Daily Breakout Screener")
st.caption("NSE cash market | Daily candles | First monthly breakout | Red-candle confirmation")

st.info(
    """
    **Rules:** The first green daily candle of the current month must close above the previous completed month's High from below.
    After it, exactly 1, 2, or 3 additional green daily candles must form, followed immediately by a red candle.
    The red candle's High must be below the original breakout candle's High.
    Only one signal per symbol per month. Today's incomplete candle is ignored.
    """
)

EQUITY_URLS = [
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
]
FO_URLS = [
    "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
    "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
]
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}


@st.cache_data(ttl=86400, show_spinner=False)
def get_symbols():
    equity = None
    for url in EQUITY_URLS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            equity = pd.read_csv(io.StringIO(response.content.decode("utf-8-sig", errors="replace")))
            if not equity.empty:
                break
        except Exception:
            continue

    if equity is None or equity.empty:
        raise RuntimeError(
            "NSE equity list download failed."
        )

    equity.columns = [str(c).strip().upper() for c in equity.columns]
    if "SYMBOL" not in equity.columns:
        raise RuntimeError("NSE list is missing the SYMBOL column.")
    if "SERIES" in equity.columns:
        equity = equity[equity["SERIES"].astype(str).str.strip().str.upper().eq("EQ")]
    cash_symbols = set(equity["SYMBOL"].dropna().astype(str).str.strip().str.upper())

    fo = None
    for url in FO_URLS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            fo = pd.read_csv(io.StringIO(response.content.decode("utf-8-sig", errors="replace")))
            if not fo.empty:
                break
        except Exception:
            continue

    if fo is None or fo.empty:
        raise RuntimeError(
            "F&O list download failed; scan stopped to avoid including F&O stocks."
        )

    fo.columns = [str(c).strip().upper() for c in fo.columns]
    fo_symbols = set()
    for column in ("SYMBOL", "UNDERLYING", "UNDERLYING_SYMBOL"):
        if column in fo.columns:
            fo_symbols.update(fo[column].dropna().astype(str).str.strip().str.upper())
    if not fo_symbols:
        raise RuntimeError("Could not identify the symbol column in the F&O list.")

    return sorted(cash_symbols - fo_symbols)


@st.cache_data(ttl=1800, show_spinner=False)
def download_daily(symbol):
    try:
        data = yf.Ticker(f"{symbol}.NS").history(
            period="9mo", interval="1d", auto_adjust=False, actions=False
        )
        if data is None or data.empty:
            return pd.DataFrame()
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [col[0] for col in data.columns]
        required = ["Open", "High", "Low", "Close"]
        if any(column not in data.columns for column in required):
            return pd.DataFrame()
        data[required] = data[required].apply(pd.to_numeric, errors="coerce")
        data = data.dropna(subset=required).sort_index()
        return data[~data.index.duplicated(keep="last")]
    except Exception:
        return pd.DataFrame()


def scan_symbol(symbol, minimum_price, maximum_price):
    data = download_daily(symbol)
    if data.empty or len(data) < 5:
        return None

    # Ignore today's possibly unfinished daily candle.
    now = pd.Timestamp.now(tz=IST)
    parsed_dates = pd.to_datetime(data.index)
    if getattr(data.index, "tz", None) is not None:
        today_mask = parsed_dates.tz_convert("Asia/Kolkata").date == now.date()
    else:
        today_mask = parsed_dates.date == now.date()
    if len(data) and bool(today_mask[-1]):
        data = data.iloc[:-1]
    if len(data) < 5:
        return None

    latest_price = float(data["Close"].iloc[-1])
    if not minimum_price <= latest_price <= maximum_price:
        return None

    dates = pd.DatetimeIndex(pd.to_datetime(data.index))
    if dates.tz is not None:
        dates = dates.tz_localize(None)

    month_start = pd.Timestamp(now.year, now.month, 1)
    previous_month_end = month_start - pd.Timedelta(days=1)
    previous_month_start = pd.Timestamp(previous_month_end.year, previous_month_end.month, 1)

    previous_month_mask = (dates >= previous_month_start) & (dates <= previous_month_end)
    previous_month = data.loc[previous_month_mask]
    if previous_month.empty:
        return None
    previous_month_high = float(previous_month["High"].max())

    current_month_positions = [
        i for i, date in enumerate(dates)
        if date >= month_start and date.date() < now.date()
    ]
    if len(current_month_positions) < 2:
        return None

    # Find only the first green breakout candle of the current calendar month.
    breakout_i = None
    for i in current_month_positions:
        if i == 0:
            continue
        candle = data.iloc[i]
        previous = data.iloc[i - 1]
        if (
            float(candle["Close"]) > float(candle["Open"])
            and float(candle["Close"]) > previous_month_high
            and float(previous["Close"]) <= previous_month_high
        ):
            breakout_i = i
            break

    if breakout_i is None:
        return None

    breakout = data.iloc[breakout_i]
    breakout_high = float(breakout["High"])

    # Valid patterns:
    # breakout green -> 1 green -> red
    # breakout green -> 2 greens -> red
    # breakout green -> 3 greens -> red
    # Red candle must immediately follow the 1st, 2nd, or 3rd green candle.
    green_count = 0
    red_i = None
    red = None

    for j in range(breakout_i + 1, len(data)):
        if dates[j].date() >= now.date():
            break
        candle = data.iloc[j]
        open_price = float(candle["Open"])
        close_price = float(candle["Close"])
        high_price = float(candle["High"])

        if close_price > open_price:
            green_count += 1
            if green_count > 3:
                break
            continue

        if close_price < open_price:
            if 1 <= green_count <= 3 and high_price < breakout_high:
                red_i = j
                red = candle
            break

        # A doji interrupts the pattern.
        break

    if red is None:
        return None

    return {
        "Symbol": symbol,
        "Latest Close (INR)": round(latest_price, 2),
        "Previous Month High (INR)": round(previous_month_high, 2),
        "Breakout Date": str(dates[breakout_i].date()),
        "Breakout Candle High (INR)": round(breakout_high, 2),
        "Breakout Candle Close (INR)": round(float(breakout["Close"]), 2),
        "Green Candles After Breakout": green_count,
        "Red Confirmation Date": str(dates[red_i].date()),
        "Red Candle High (INR)": round(float(red["High"]), 2),
        "Red Candle Close (INR)": round(float(red["Close"]), 2),
    }


with st.sidebar:
    st.header("Scan Settings")
    min_price = st.number_input("Minimum Price (INR)", min_value=0.0, value=100.0, step=10.0)
    max_price = st.number_input("Maximum Price (INR)", min_value=1.0, value=5000.0, step=100.0)
    scan_size = st.selectbox(
        "Stocks to Scan",
        [
            "All eligible cash stocks",
            "First 100 (test)",
            "First 250",
        ],
    )
    st.caption("Full scan may take several minutes. Market data may be delayed or incomplete.")

if min_price > max_price:
    st.error("Minimum price must not exceed maximum price.")
    st.stop()

metric1, metric2, metric3 = st.columns(3)
metric1.metric("Timeframe", "Daily")
metric2.metric("Price Range", f"INR {min_price:g} - {max_price:g}")
metric3.metric("Market", "NSE Cash (F&O excluded)")

if st.button("Scan Stocks Now", type="primary", use_container_width=True):
    try:
        with st.spinner("Loading NSE symbol lists..."):
            symbols = get_symbols()

        if scan_size.startswith("First 100"):
            symbols = symbols[:100]
        elif scan_size.startswith("First 250"):
            symbols = symbols[:250]

        st.write(f"**Stocks to scan:** {len(symbols)}")
        results = []
        failures = 0
        progress = st.progress(0)
        status = st.empty()

        for number, symbol in enumerate(symbols, start=1):
            status.write(f"Checking **{symbol}** ({number}/{len(symbols)})")
            try:
                match = scan_symbol(symbol, min_price, max_price)
                if match:
                    results.append(match)
            except Exception:
                failures += 1
            progress.progress(number / max(1, len(symbols)))

        status.empty()
        progress.empty()
        result_df = pd.DataFrame(results)

        if not result_df.empty:
            result_df = result_df.sort_values(
                ["Red Candle Date", "Symbol"],
                ascending=[False, True],
            ).reset_index(drop=True)
            st.success(
                f"Scan complete: {len(result_df)} matching stock(s)."
            )
            st.dataframe(result_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download CSV",
                data=result_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="nse_daily_breakout_results.csv",
                mime="text/csv",
                use_container_width=True,
            )
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                result_df.to_excel(writer, index=False, sheet_name="Results")
            st.download_button(
                "Download Excel",
                data=excel_buffer.getvalue(),
                file_name="nse_daily_breakout_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.warning(
                "No stocks matched the rules in this scan."
            )
        if failures:
            st.caption(
                f"{failures} symbol(s) could not be checked due to data/network errors."
            )
    except Exception as error:
        st.error(f"Scan failed: {error}")
else:
    st.write(
        "Set the price range in the sidebar and press Scan Stocks Now."
    )
    st.caption(
        "Results will appear here. CSV and Excel download buttons appear after the scan."
    )

with st.expander("Scanner Rules"):
    st.markdown(
        """
        1. Scan NSE EQ cash-market symbols and exclude symbols in the NSE F&O list.
        2. Latest completed daily close must be within the selected price range.
        3. The breakout candle is the first green daily candle in the current month that closes above the previous completed calendar month's High from below.
        4. After the breakout candle, the valid sequence is exactly 1, 2, or 3 additional green daily candles followed immediately by a red candle.
        5. The red candle's High must be lower than the original breakout candle's High.
        6. Only one signal per symbol per month; the setup resets when a new month starts.
        7. Today's incomplete candle is ignored.
        """
    )

st.caption("Educational screening tool only â€” not a buy/sell recommendation.")
