import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NSE Breakout Screener | à¤¶à¥‡à¤¯à¤° à¤¸à¥à¤•à¥ˆà¤¨à¤°",
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
st.caption("NSE cash market â€¢ Daily candles â€¢ Previous-month high breakout â€¢ Next-day red confirmation")

st.info(
    """
    **Setup rules:** The green daily candle's Close must be above the previous completed calendar month's High.
    The immediately following completed daily candle must be red, and its High must be below the green candle's High.
    Today's incomplete candle is ignored.
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
    if data.empty or len(data) < 3:
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
    if len(data) < 2:
        return None

    latest_price = float(data["Close"].iloc[-1])
    if not minimum_price <= latest_price <= maximum_price:
        return None

    month_start = pd.Timestamp(now.year, now.month, 1)
    previous_month_end = month_start - pd.Timedelta(days=1)
    previous_month_start = pd.Timestamp(previous_month_end.year, previous_month_end.month, 1)
    dates = pd.to_datetime(data.index)
    if getattr(data.index, "tz", None) is not None:
        dates = dates.tz_localize(None)
    previous_month = data.loc[
        (dates >= previous_month_start) & (dates <= previous_month_end)
    ]
    if previous_month.empty:
        return None
    previous_month_high = float(previous_month["High"].max())

    # Search the most recent adjacent completed daily-candle pair.
    for i in range(len(data) - 1, 0, -1):
        green = data.iloc[i - 1]
        red = data.iloc[i]
        if (
            float(green["Close"]) > float(green["Open"])
            and float(green["Close"]) > previous_month_high
            and float(red["Close"]) < float(red["Open"])
            and float(red["High"]) < float(green["High"])
        ):
            return {
                "Symbol": symbol,
                "Latest Close (â‚¹)": round(latest_price, 2),
                "Previous Month High (â‚¹)": round(previous_month_high, 2),
                "Green Candle Date": str(pd.Timestamp(data.index[i - 1]).date()),
                "Green Candle High (â‚¹)": round(float(green["High"]), 2),
                "Green Candle Close (â‚¹)": round(float(green["Close"]), 2),
                "Red Candle Date": str(pd.Timestamp(data.index[i]).date()),
                "Red Candle High (â‚¹)": round(float(red["High"]), 2),
                "Red Candle Close (â‚¹)": round(float(red["Close"]), 2),
            }
    return None


with st.sidebar:
    st.header("Scan Settings")
    min_price = st.number_input("Minimum Price (â‚¹)", min_value=0.0, value=100.0, step=10.0)
    max_price = st.number_input("Maximum Price (â‚¹)", min_value=1.0, value=5000.0, step=100.0)
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
metric2.metric("Price Range", f"â‚¹{min_price:g}â€“â‚¹{max_price:g}")
metric3.metric("Market", "NSE Cash (F&O excluded)")

if st.button("ðŸ”Ž Scan Stocks Now", type="primary", use_container_width=True):
    try:
        with st.spinner("Loading NSE symbol lists..."):
            symbols = get_symbols()

        if scan_size.startswith("First 100"):
            symbols = symbols[:100]
        elif scan_size.startswith("First 250"):
            symbols = symbols[:250]

        st.write(f"**Stocks to scan / à¤¸à¥à¤•à¥ˆà¤¨ à¤¹à¥‹à¤¨à¥‡ à¤µà¤¾à¤²à¥‡ à¤¶à¥‡à¤¯à¤°:** {len(symbols)}")
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
                "â¬‡ï¸ Download CSV",
                data=result_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="nse_daily_breakout_results.csv",
                mime="text/csv",
                use_container_width=True,
            )
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                result_df.to_excel(writer, index=False, sheet_name="Results")
            st.download_button(
                "â¬‡ï¸ Download Excel",
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

with st.expander("ðŸ“˜ Scanner Rules"):
    st.markdown(
        """
        1. NSE EQ cash-market symbols are included; symbols listed in the NSE F&O list are excluded.
        2. The latest completed daily close must be within the selected price range.
        3. Green candle: Close > Open and Close > the previous completed calendar month's High.
        4. The immediately following completed daily candle must be red: Close < Open.
        5. The red candle's High must be lower than the green breakout candle's High.
        6. Today's incomplete candle is ignored.
        """
    )

st.caption("Educational screening tool only â€” not a buy/sell recommendation.")
