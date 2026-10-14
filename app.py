import io
import pandas as pd
import streamlit as st
import yfinance as yf
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="NSE Daily Breakout Screener", page_icon="ðŸ“ˆ", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 2rem;}
.small-note {color: #777; font-size: 0.85rem;}
</style>
""", unsafe_allow_html=True)

st.title("ðŸ“ˆ NSE Daily Breakout Screener")
st.caption("NSE cash market â€¢ Daily candles â€¢ Previous-month high breakout â€¢ Next-day red confirmation")

st.info(
    "Setup: Green daily candle Close > previous completed calendar month's High. "
    "The immediately following completed daily candle must be red, and its High must be "
    "below the green breakout candle's High. Today's incomplete candle is ignored."
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
    errors = []
    for url in EQUITY_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            equity = pd.read_csv(io.StringIO(r.text))
            if not equity.empty:
                break
        except Exception as e:
            errors.append(str(e))
    if equity is None or equity.empty:
        raise RuntimeError("NSE equity list download failed. " + "; ".join(errors))

    equity.columns = [str(c).strip().upper() for c in equity.columns]
    if "SYMBOL" not in equity.columns:
        raise RuntimeError("NSE equity list mein SYMBOL column nahi mila.")
    if "SERIES" in equity.columns:
        equity = equity[equity["SERIES"].astype(str).str.strip().str.upper().eq("EQ")]
    cash_symbols = set(equity["SYMBOL"].dropna().astype(str).str.strip().str.upper())

    fo = None
    errors = []
    for url in FO_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            fo = pd.read_csv(io.StringIO(r.text))
            if not fo.empty:
                break
        except Exception as e:
            errors.append(str(e))
    if fo is None or fo.empty:
        raise RuntimeError("NSE F&O list download failed; F&O ko safely exclude nahi kar sakte. " + "; ".join(errors))

    fo.columns = [str(c).strip().upper() for c in fo.columns]
    fo_symbols = set()
    for col in ("SYMBOL", "UNDERLYING", "UNDERLYING_SYMBOL"):
        if col in fo.columns:
            fo_symbols.update(fo[col].dropna().astype(str).str.strip().str.upper())
    if not fo_symbols:
        raise RuntimeError("F&O file mein symbol column identify nahi hua; scan roka gaya.")
    return sorted(cash_symbols - fo_symbols)

@st.cache_data(ttl=1800, show_spinner=False)
def download_daily(symbol):
    try:
        df = yf.Ticker(f"{symbol}.NS").history(
            period="9mo", interval="1d", auto_adjust=False, actions=False
        )
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        required = ["Open", "High", "Low", "Close"]
        if any(c not in df.columns for c in required):
            return pd.DataFrame()
        df[required] = df[required].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=required).sort_index()
        return df[~df.index.duplicated(keep="last")]
    except Exception:
        return pd.DataFrame()

def scan_symbol(symbol, min_price, max_price):
    df = download_daily(symbol)
    if df.empty or len(df) < 3:
        return None

    # Exclude today's candle while it may still be forming.
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    dates = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        today_mask = dates.tz_convert("Asia/Kolkata").date == now.date()
    else:
        today_mask = dates.date == now.date()
    if len(df) and bool(today_mask[-1]):
        df = df.iloc[:-1]
    if len(df) < 2:
        return None

    latest_price = float(df["Close"].iloc[-1])
    if not min_price <= latest_price <= max_price:
        return None

    # High of the previous fully completed calendar month.
    first_this_month = pd.Timestamp(now.year, now.month, 1)
    last_prev_month = first_this_month - pd.Timedelta(days=1)
    first_prev_month = pd.Timestamp(last_prev_month.year, last_prev_month.month, 1)
    idx_dates = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        idx_dates = idx_dates.tz_localize(None)
    prev_month = df.loc[(idx_dates >= first_prev_month) & (idx_dates <= last_prev_month)]
    if prev_month.empty:
        return None
    prev_month_high = float(prev_month["High"].max())

    # Search recent adjacent completed daily candles.
    # Return the latest pair that matches the requested setup.
    for i in range(len(df) - 1, 0, -1):
        green = df.iloc[i - 1]
        red = df.iloc[i]
        green_date = pd.Timestamp(df.index[i - 1]).date()
        red_date = pd.Timestamp(df.index[i]).date()

        if (
            float(green["Close"]) > float(green["Open"])
            and float(green["Close"]) > prev_month_high
            and float(red["Close"]) < float(red["Open"])
            and float(red["High"]) < float(green["High"])
        ):
            return {
                "Symbol": symbol,
                "Latest Close (â‚¹)": round(latest_price, 2),
                "Previous Month High (â‚¹)": round(prev_month_high, 2),
                "Breakout Green Date": str(green_date),
                "Breakout Green High (â‚¹)": round(float(green["High"]), 2),
                "Breakout Green Close (â‚¹)": round(float(green["Close"]), 2),
                "Confirmation Red Date": str(red_date),
                "Confirmation Red High (â‚¹)": round(float(red["High"]), 2),
                "Confirmation Red Close (â‚¹)": round(float(red["Close"]), 2),
            }
    return None

with st.sidebar:
    st.header("âš™ï¸ Scan Settings")
    min_price = st.number_input("Minimum price (â‚¹)", min_value=0.0, value=100.0, step=10.0)
    max_price = st.number_input("Maximum price (â‚¹)", min_value=1.0, value=5000.0, step=100.0)
    scan_size = st.selectbox("Stocks to scan", ["All eligible cash stocks", "First 100 (test)", "First 250"])
    st.caption("Full scan may take several minutes. Market data may be delayed or incomplete.")

if min_price > max_price:
    st.error("Minimum price maximum price se zyada nahi ho sakta.")
    st.stop()

col1, col2, col3 = st.columns(3)
col1.metric("Timeframe", "Daily")
col2.metric("Price Range", f"â‚¹{min_price:g}â€“â‚¹{max_price:g}")
col3.metric("Universe", "NSE Cash, F&O excluded")

if st.button("ðŸ”Ž Scan Stocks Now", type="primary", use_container_width=True):
    try:
        with st.spinner("NSE symbol lists load ho rahi hain..."):
            symbols = get_symbols()
        if scan_size == "First 100 (test)":
            symbols = symbols[:100]
        elif scan_size == "First 250":
            symbols = symbols[:250]

        st.write(f"**Stocks to scan:** {len(symbols)}")
        results, failures = [], 0
        progress = st.progress(0)
        status = st.empty()

        for n, symbol in enumerate(symbols, start=1):
            status.write(f"Checking **{symbol}** ({n}/{len(symbols)})")
            try:
                match = scan_symbol(symbol, min_price, max_price)
                if match:
                    results.append(match)
            except Exception:
                failures += 1
            progress.progress(n / max(1, len(symbols)))

        status.empty()
        progress.empty()
        result_df = pd.DataFrame(results)

        if not result_df.empty:
            result_df = result_df.sort_values(
                ["Confirmation Red Date", "Symbol"], ascending=[False, True]
            ).reset_index(drop=True)
            st.success(f"Scan complete: {len(result_df)} matching stock(s).")
            st.dataframe(result_df, use_container_width=True, hide_index=True)
            st.download_button(
                "â¬‡ï¸ Download CSV",
                data=result_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="nse_daily_breakout_results.csv",
                mime="text/csv",
                use_container_width=True,
            )
            excel = io.BytesIO()
            with pd.ExcelWriter(excel, engine="openpyxl") as writer:
                result_df.to_excel(writer, index=False, sheet_name="Breakout Results")
            st.download_button(
                "â¬‡ï¸ Download Excel",
                data=excel.getvalue(),
                file_name="nse_daily_breakout_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.warning("Is scan mein koi stock in rules se match nahi hua.")
        if failures:
            st.caption(f"{failures} symbols data/network error ke kaaran check nahi ho sake.")
    except Exception as e:
        st.error(f"Scan start nahi ho saka: {e}")
else:
    st.write("Sidebar mein price range set karke **Scan Stocks Now** dabayein.")
    st.caption("Results isi page par table mein aayenge. CSV aur Excel download buttons scan ke baad dikhenge.")

with st.expander("ðŸ“˜ Scanner Rules"):
    st.markdown("""
    1. NSE equity list mein EQ series ke shares.
    2. NSE F&O list mein maujood symbols exclude kiye jaate hain.
    3. Latest completed daily close configured price range mein hona chahiye.
    4. Green candle: Close > Open aur Close > previous completed calendar month's High.
    5. Immediately next completed daily candle red honi chahiye: Close < Open.
    6. Red candle High < green breakout candle High.
    7. Aaj ki incomplete daily candle ignore hoti hai.
    8. Data source: Yahoo Finance; results ko trading se pehle independently verify karein.
    """)
st.caption("Educational screening tool only â€” buy/sell recommendation nahi hai.")
