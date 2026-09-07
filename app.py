import time
import threading
import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures
from datetime import date, timedelta

# --- セッションステートの初期化 ---
if "market_filter" not in st.session_state:
    st.session_state.market_filter = "すべて"
if "sector_filter" not in st.session_state:
    st.session_state.sector_filter = "すべて"
if "data_source" not in st.session_state:
    st.session_state.data_source = "yfinance"
if "use_ytd_low" not in st.session_state:
    st.session_state.use_ytd_low = True
if "use_decline" not in st.session_state:
    st.session_state.use_decline = True
if "use_ore_teki" not in st.session_state:
    st.session_state.use_ore_teki = False
if "exclude_today" not in st.session_state:
    st.session_state.exclude_today = True

# --- スクリーニング条件（固定値） ---
LOOKBACK_DAYS = 20
MIN_AVG_VOLUME = 10000
DECLINE_THRESHOLD_PCT = 20.0  # 直近3ヶ月の高値からの下落率（約20%以上）
DECLINE_LOOKBACK_DAYS = 92  # 「直近3ヶ月」の目安（暦日ベース）
ORE_TEKI_PRICE_MIN = 1000.0  # 「俺的株」の株価下限（円）
ORE_TEKI_PRICE_MAX = 2000.0  # 「俺的株」の株価上限（円）

# --- ページ設定 ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide",
)

# --- 設定 (Discord Webhook) ---
try:
    DISCORD_WEBHOOK_URL = st.secrets["DISCORD_WEBHOOK_URL"]
except Exception:
    DISCORD_WEBHOOK_URL = ""

# --- 設定 (J-Quants API Key) ---
try:
    JQUANTS_API_KEY_SECRET = st.secrets["JQUANTS_API_KEY"]
except Exception:
    JQUANTS_API_KEY_SECRET = ""

JQUANTS_BASE_URL = "https://api.jquants.com/v2"

# Lightプランのレートリミットは 60 リクエスト/分。
# 大幅に超過すると5分程度アクセスが完全に遮断されるため、余裕をもって抑える。
JQ_RATE_LIMIT_PER_MIN = 55
JQ_MAX_WORKERS = 4  # 実効速度はレートリミッタで決まるので同時実行数は控えめでよい


# ============================================================
# 共通ユーティリティ
# ============================================================

def normalize_code(value):
    """
    data_j.xls の「コード」列を文字列コードに正規化する。
    Excel読み込み時に float 化されて '1301.0' になるケースと、
    英文字入り証券コード（例: '130A'）の両方に対応する。
    """
    if pd.isna(value):
        return ""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def to_4digit(code5):
    """
    J-Quantsの5桁コード（例: '86970'）を data_j.xls の4桁コード（'8697'）に変換する。
    末尾が '0' でないものは優先株・優先出資証券等なので除外する（None を返す）。
    """
    s = str(code5)
    if len(s) == 5 and s.endswith("0"):
        return s[:4]
    return None


class RateLimiter:
    """プロセス全体で1分あたりのリクエスト数を平準化する簡易リミッタ。"""

    def __init__(self, max_per_min):
        self.interval = 60.0 / float(max_per_min)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.interval


def classify_size(scale_label):
    """
    JPXの規模区分（TOPIX Core30 / Large70 / Mid400 / Small 1 / Small 2 等）から、
    大型株（TOPIX100=Core30+Large70）／中型株（TOPIX Mid400）／小型株（それ以外）に分類する。
    """
    if not isinstance(scale_label, str):
        return "小型株"
    if scale_label in ("TOPIX Core30", "TOPIX Large70"):
        return "大型株"
    if scale_label == "TOPIX Mid400":
        return "中型株"
    return "小型株"


@st.cache_data(ttl=86400)
def load_jpx_data():
    try:
        df = pd.read_excel("data_j.xls")
        df = df[df['市場・商品区分'].notna()]

        # ETF・ETN・REIT・インフラファンド・出資証券などの投資信託系を除外し、
        # 普通株式（プライム／スタンダード／グロース＝「内国株式」）のみを対象にする
        df = df[df['市場・商品区分'].str.contains('内国株式', na=False)]

        if '規模区分' in df.columns:
            df['規模カテゴリ'] = df['規模区分'].apply(classify_size)
        return df
    except Exception as e:
        st.error(f"銘柄データの取得に失敗しました: data_j.xls ファイルを確認してください: {e}")
        return pd.DataFrame()


# ============================================================
# データ取得レイヤー（yfinance / J-Quants）
# ============================================================

def fetch_yfinance_history_raw(code, from_date_str, to_date_str):
    """
    yfinanceから日足データ（High, Low, Close, Volume）を取得する。

    auto_adjust=False を明示している点が重要。
    - auto_adjust=True（新しめのyfinanceのデフォルト）だと配当込みで遡及調整された値になる
    - J-Quantsの調整は株式分割・併合・ライツイシューのみで、配当は対象外
    auto_adjust=False の場合、Yahooの High/Low/Close は分割調整済み・配当未調整なので、
    J-Quantsの AdjH/AdjL/AdjC と調整の意味が揃う。
    """
    try:
        ticker = yf.Ticker(f"{code}.T")
        # yfinanceの end は排他的なので、to_date当日を含めるため +1日する
        end_exclusive = (pd.to_datetime(to_date_str) + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = ticker.history(start=from_date_str, end=end_exclusive, auto_adjust=False)
        if hist.empty:
            return pd.DataFrame()
        hist = hist[["High", "Low", "Close", "Volume"]].copy()
        hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()
        return hist
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_yfinance_history(code, from_date_str, to_date_str):
    return fetch_yfinance_history_raw(code, from_date_str, to_date_str)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_per_yfinance(code):
    """
    PER（株価収益率）をyfinanceから取得する。
    ※ J-Quants Lightプランでも財務情報（/fins/summary）は取得できるため、
      本来はJ-Quants側のEPSと時価総額（日足のMktCap列）から自前計算もできる。
      ここでは変更範囲を絞るため従来どおりyfinanceから取得している。
    """
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        per = info.get('trailingPE') or info.get('forwardPE')
        if not per:
            current_price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
            eps = info.get('trailingEps') or info.get('forwardEps')
            if current_price and eps and eps > 0:
                per = current_price / eps
        return per
    except Exception:
        return None


def jq_request(path, params, api_key, limiter, errors, max_retries=4):
    """
    J-Quants API v2 への1リクエスト（pagination_key があれば続きも取得）。

    旧実装との違い：
      - 非200を黙って握り潰さず errors に積む（原因が画面に出るようになる）
      - 429 はバックオフしてリトライする
      - 全リクエストを RateLimiter で平準化する
    """
    url = JQUANTS_BASE_URL + path
    headers = {"x-api-key": api_key}
    params = dict(params)
    records = []
    pagination_key = None

    while True:
        if pagination_key:
            params["pagination_key"] = pagination_key

        res = None
        for attempt in range(max_retries):
            limiter.acquire()
            try:
                res = requests.get(url, headers=headers, params=params, timeout=30)
            except Exception as e:
                errors.append(f"{path} {params.get('date') or params.get('code')}: 通信エラー {e}")
                return records

            if res.status_code == 200:
                break
            if res.status_code == 429:
                # レートリミット超過。J-Quantsはリセットが長めなので線形に待つ。
                time.sleep(30 * (attempt + 1))
                res = None
                continue
            errors.append(
                f"{path} {params.get('date') or params.get('code')}: "
                f"HTTP {res.status_code} {res.text[:200]}"
            )
            return records

        if res is None:
            errors.append(f"{path} {params.get('date') or params.get('code')}: 429が続いたため中断")
            return records

        payload = res.json()
        records.extend(payload.get("data", []))
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jquants_panel(from_date_str, to_date_str, api_key, _progress=None):
    """
    指定期間の「全上場銘柄」の日足を、日付ごとに一括取得する。

    ここが今回の最大の修正点。
    /equities/bars/daily は date だけ指定すれば全上場銘柄が1リクエストで取れる。
    銘柄コードごとに叩くと約4000リクエストになり、Lightの60req/分では
    確実に429で全滅する（=結果が0件になる）。
    日付ループなら年初来でも約170リクエストで済む。

    戻り値: ({4桁コード: 日足DataFrame}, エラーのリスト)
    """
    if not api_key:
        return {}, ["APIキーが設定されていません。"]

    # 土日は最初から除外する（祝日は空レスポンスが返るだけなので許容）
    dates = pd.bdate_range(from_date_str, to_date_str)
    if len(dates) == 0:
        return {}, []

    limiter = RateLimiter(JQ_RATE_LIMIT_PER_MIN)
    errors = []
    all_records = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=JQ_MAX_WORKERS) as ex:
        futures = [
            ex.submit(
                jq_request,
                "/equities/bars/daily",
                {"date": d.strftime("%Y-%m-%d")},
                api_key,
                limiter,
                errors,
            )
            for d in dates
        ]
        for i, fut in enumerate(concurrent.futures.as_completed(futures)):
            all_records.extend(fut.result())
            if _progress:
                _progress((i + 1) / len(futures), i + 1, len(futures))

    if not all_records:
        return {}, errors

    df = pd.DataFrame(all_records)
    if "Date" not in df.columns or "Code" not in df.columns:
        errors.append("レスポンスに Date / Code 列がありません。API仕様を確認してください。")
        return {}, errors

    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df["Code4"] = df["Code"].astype(str).map(to_4digit)
    df = df[df["Code4"].notna()]

    # 調整済み（分割・併合・ライツイシュー調整）の High / Low / Close / Volume を使う
    df = df.rename(columns={"AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"})
    need = ["Date", "Code4", "High", "Low", "Close", "Volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        errors.append(f"レスポンスに必要な列がありません: {missing}")
        return {}, errors

    df = df[need].sort_values("Date")

    panel = {}
    for code4, g in df.groupby("Code4", sort=False):
        panel[code4] = g.set_index("Date")[["High", "Low", "Close", "Volume"]]

    return panel, errors


def fetch_jquants_single(code, from_date_str, to_date_str, api_key):
    """単一銘柄の日足（全銘柄一覧タブの個別検索用。1リクエストなので code 指定でよい）。"""
    limiter = RateLimiter(JQ_RATE_LIMIT_PER_MIN)
    errors = []
    records = jq_request(
        "/equities/bars/daily",
        {"code": code, "from": from_date_str, "to": to_date_str},
        api_key,
        limiter,
        errors,
    )
    if errors:
        st.warning(" / ".join(errors[:3]))
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if "Date" not in df.columns:
        return pd.DataFrame()
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df = df.sort_values("Date").set_index("Date")
    df = df.rename(columns={"AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"})
    keep = [c for c in ["High", "Low", "Close", "Volume"] if c in df.columns]
    return df[keep]


def trim_today(hist, exclude_today):
    """
    当日分を落とす。
    J-Quantsの日足は大引け後に更新されるのに対し、yfinanceは場中の途中経過が入る。
    これを揃えないと「年初来安値更新」の判定が両者で食い違う。
    """
    if not exclude_today or hist is None or hist.empty:
        return hist
    today_ts = pd.Timestamp(date.today())
    return hist[hist.index < today_ts]


# ============================================================
# スクリーニング条件の判定
# ============================================================

def compute_from_date(lookback_days, decline_lookback_days, use_ytd_low, need_decline_data):
    """全銘柄で共通の取得開始日を決める（従来は銘柄ごとに計算していた）。"""
    today = date.today()
    jan1 = date(today.year, 1, 1)
    from_candidates = [today - timedelta(days=int(lookback_days * 2.5) + 10)]
    if use_ytd_low:
        from_candidates.append(jan1)
    if need_decline_data:
        from_candidates.append(today - timedelta(days=decline_lookback_days))
    return min(from_candidates)


def screen_hist(
    code,
    hist,
    min_avg_volume,
    lookback_days,
    decline_threshold_pct,
    decline_lookback_days,
    use_ytd_low,
    use_decline,
    use_ore_teki,
    price_min,
    price_max,
):
    """
    取得済みの日足に対して条件判定する（データ取得と判定を分離した）。
      1. 直近N日平均出来高が下限以上（常に適用する足切り条件）
      2. use_ytd_low: 当日の安値が年初来安値を更新しているか
      3. use_decline / use_ore_teki: 直近decline_lookback_days日間の高値からの下落率
      4. use_ore_teki: 現在値がprice_min〜price_max円の範囲内か
    """
    if hist is None or hist.empty:
        return None

    today = date.today()
    jan1 = date(today.year, 1, 1)
    decline_from = today - timedelta(days=decline_lookback_days)

    need_decline_data = use_decline or use_ore_teki

    # 1. 出来高条件（足切り）
    if len(hist) < lookback_days:
        return None
    avg_volume = hist['Volume'].tail(lookback_days).mean()
    if pd.isna(avg_volume) or avg_volume < min_avg_volume:
        return None

    ytd_low_hit = None
    decline_pct = None
    latest_close = None

    # 2. 年初来安値更新の条件
    if use_ytd_low:
        ytd_hist = hist[hist.index.date >= jan1]
        if len(ytd_hist) < 2:
            return None
        ytd_low = ytd_hist['Low'].min()
        latest_low = ytd_hist['Low'].iloc[-1]
        if pd.isna(ytd_low) or pd.isna(latest_low):
            return None
        ytd_low_hit = latest_low <= ytd_low
        if not ytd_low_hit:
            return None

    # 3. 直近3ヶ月の高値からの下落率の条件
    if need_decline_data:
        if 'Close' not in hist.columns:
            return None
        recent_hist = hist[hist.index.date >= decline_from]
        if len(recent_hist) < 2:
            return None
        recent_high = recent_hist['High'].max()
        latest_close = hist['Close'].iloc[-1]
        if pd.isna(recent_high) or pd.isna(latest_close) or recent_high <= 0:
            return None
        decline_pct = (recent_high - latest_close) / recent_high * 100
        if decline_pct < decline_threshold_pct:
            return None

    # 4. 「俺的株」の株価レンジ条件
    if use_ore_teki:
        if latest_close is None:
            if 'Close' not in hist.columns:
                return None
            latest_close = hist['Close'].iloc[-1]
        if pd.isna(latest_close) or not (price_min <= latest_close <= price_max):
            return None

    return {
        "code": code,
        "avg_volume": avg_volume,
        "ytd_low_hit": ytd_low_hit,
        "decline_pct": decline_pct,
        "latest_close": latest_close,
    }


def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})


def tradingview_symbol_url(code):
    return f"https://jp.tradingview.com/symbols/TSE-{code}/"


GEMINI_URL = "https://gemini.google.com/app"


def build_fundamental_prompt(company_name, code):
    return (
        f"{company_name}（証券コード: {code}、東京証券取引所上場）について、"
        f"直近の業績・財務状況をふまえたファンダメンタル分析をお願いします。\n"
        f"・強み、割安感、注目ポイントを3行程度で\n"
        f"・年初来安値を更新し、出来高が増えている背景として考えられる要因も教えてください"
    )


def render_company_card(company_name, code, key_prefix, caption_parts=None):
    tv_url = tradingview_symbol_url(code)
    with st.container(border=True):
        st.markdown(
            f"#### [{company_name}]({tv_url}) "
            f"<span style='font-size:0.8em; color:gray;'>({code})</span>",
            unsafe_allow_html=True,
        )
        if caption_parts:
            st.caption(" ｜ ".join(caption_parts))
        st.caption(
            "👆 企業名をタップするとTradingView（日本語）のページを開きます"
            "（アプリがあれば自動的にアプリが開きます）。開いたら「財務」タブで財務情報を確認できます。"
        )

        gc1, gc2 = st.columns([1, 2])
        with gc1:
            st.link_button(
                "🤖 Geminiで調べる",
                GEMINI_URL,
                use_container_width=True,
                key=f"{key_prefix}_gemini_btn_{code}",
            )
        with gc2:
            with st.expander(f"📋 Geminiに貼り付ける質問文をコピー（{company_name}）"):
                st.code(build_fundamental_prompt(company_name, code), language="markdown")


# --- データ読み込み ---
df_jpx = load_jpx_data()
if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].apply(normalize_code)
    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())

# --- サイドバー：データソース設定 ---
st.sidebar.header("⚙️ データソース設定")
st.session_state.data_source = st.sidebar.radio(
    "株価データの取得元",
    ["yfinance", "J-Quants"],
    index=0 if st.session_state.data_source == "yfinance" else 1,
    help="yfinanceは無料・登録不要ですが、J-Quantsは東証公式データでより正確です（要APIキー登録）。",
)

jquants_api_key = JQUANTS_API_KEY_SECRET
if st.session_state.data_source == "J-Quants":
    if JQUANTS_API_KEY_SECRET:
        st.sidebar.success("secretsに設定されたJ-Quants APIキーを使用します。")
    else:
        jquants_api_key = st.sidebar.text_input(
            "J-Quants APIキー",
            type="password",
            help="J-Quantsダッシュボード（設定 » APIキー）から取得したキーを入力してください。"
                 " .streamlit/secrets.toml に JQUANTS_API_KEY を設定しておけば毎回入力不要になります。",
        )
        if not jquants_api_key:
            st.sidebar.warning("APIキーが未入力のため、J-Quantsでのデータ取得はできません。")

st.session_state.exclude_today = st.sidebar.checkbox(
    "当日分を除外して判定する",
    value=st.session_state.exclude_today,
    help="J-Quantsの日足は大引け後に更新されるのに対し、yfinanceは場中の途中経過が入ります。"
         "両者の結果を揃えたい場合はONにしてください。",
)

st.sidebar.caption(
    f"※ J-Quantsは日付指定で全銘柄をまとめて取得します（Lightの60req/分に対し"
    f"{JQ_RATE_LIMIT_PER_MIN}req/分で平準化）。初回は数分かかりますが、以降は1時間キャッシュされます。"
)

# --- メイン画面：フィルターバー ---
st.title("📈 株式スクリーニングダッシュボード")
st.markdown("条件を設定してスクリーニングを実行するか、全銘柄一覧タブをご確認ください。")

with st.container(border=True):
    st.markdown("##### 🎛️ フィルターバー")

    f1, f2 = st.columns(2)
    with f1:
        st.session_state.market_filter = st.selectbox(
            "市場区分",
            market_options,
            index=market_options.index(st.session_state.market_filter)
            if st.session_state.market_filter in market_options else 0,
        )
    with f2:
        st.session_state.sector_filter = st.selectbox(
            "業種",
            sector_options,
            index=sector_options.index(st.session_state.sector_filter)
            if st.session_state.sector_filter in sector_options else 0,
        )

    st.markdown("---")

    st.markdown("###### 📉 スクリーニング条件")
    c1, c2 = st.columns(2)
    with c1:
        st.session_state.use_ytd_low = st.checkbox(
            "年初来安値更新（当日の安値が年初来安値を更新）",
            value=st.session_state.use_ytd_low,
        )
    with c2:
        st.session_state.use_decline = st.checkbox(
            f"直近3ヶ月の高値からの下落率が約{DECLINE_THRESHOLD_PCT:.0f}%以上",
            value=st.session_state.use_decline,
        )
    st.session_state.use_ore_teki = st.checkbox(
        f"🎯 俺的株（下落率約{DECLINE_THRESHOLD_PCT:.0f}%以上 かつ 株価"
        f"{ORE_TEKI_PRICE_MIN:,.0f}〜{ORE_TEKI_PRICE_MAX:,.0f}円で買いやすいもの）",
        value=st.session_state.use_ore_teki,
    )

    st.markdown("")
    search_btn = st.button("🚀 スクリーニングを実行する", type="primary", use_container_width=True)

st.markdown("---")
tab_screen, tab_list = st.tabs(["🔍 スクリーニング結果", "📋 全銘柄一覧"])

# ============================================================
# タブ1: スクリーニング結果
# ============================================================
with tab_screen:
    if search_btn and not df_jpx.empty:
        if st.session_state.data_source == "J-Quants" and not jquants_api_key:
            st.error("J-Quantsを選択している場合はAPIキーが必要です。サイドバーから入力してください。")
        elif not st.session_state.use_ytd_low and not st.session_state.use_decline and not st.session_state.use_ore_teki:
            st.warning("⚠️ 「年初来安値更新」「下落率」「俺的株」のいずれか1つ以上にチェックを入れてください。")
        else:
            target_df = df_jpx.copy()

            if st.session_state.market_filter != "すべて":
                target_df = target_df[target_df['市場・商品区分'] == st.session_state.market_filter]
            if st.session_state.sector_filter != "すべて":
                target_df = target_df[target_df['33業種区分'] == st.session_state.sector_filter]

            codes = target_df['コード_str'].tolist()

            if len(codes) == 0:
                st.warning("⚠️ 条件に合致する銘柄がありませんでした。")
            else:
                need_decline_data = st.session_state.use_decline or st.session_state.use_ore_teki
                from_dt = compute_from_date(
                    LOOKBACK_DAYS,
                    DECLINE_LOOKBACK_DAYS,
                    st.session_state.use_ytd_low,
                    need_decline_data,
                )
                today = date.today()
                from_str = from_dt.strftime("%Y-%m-%d")
                to_str = today.strftime("%Y-%m-%d")

                screen_kwargs = dict(
                    min_avg_volume=MIN_AVG_VOLUME,
                    lookback_days=LOOKBACK_DAYS,
                    decline_threshold_pct=DECLINE_THRESHOLD_PCT,
                    decline_lookback_days=DECLINE_LOOKBACK_DAYS,
                    use_ytd_low=st.session_state.use_ytd_low,
                    use_decline=st.session_state.use_decline,
                    use_ore_teki=st.session_state.use_ore_teki,
                    price_min=ORE_TEKI_PRICE_MIN,
                    price_max=ORE_TEKI_PRICE_MAX,
                )

                screen_results = []
                fetch_errors = []

                if st.session_state.data_source == "J-Quants":
                    progress_text = "J-Quantsから全銘柄の日足を取得中（日付ごとに一括取得）..."
                    my_bar = st.progress(0, text=progress_text)

                    def _cb(ratio, done, total):
                        my_bar.progress(min(ratio, 1.0), text=f"{progress_text} ({done}/{total}営業日)")

                    panel, fetch_errors = fetch_jquants_panel(
                        from_str, to_str, jquants_api_key, _progress=_cb
                    )
                    my_bar.empty()

                    if fetch_errors:
                        st.error(
                            "⚠️ J-Quantsの取得で "
                            f"{len(fetch_errors)}件のエラーが発生しました。結果が不完全な可能性があります。"
                        )
                        with st.expander("エラー詳細を表示"):
                            for e in fetch_errors[:30]:
                                st.text(e)

                    if not panel:
                        st.error(
                            "J-Quantsからデータを取得できませんでした。"
                            "APIキー・プラン・レートリミットを確認してください。"
                        )
                    else:
                        st.caption(f"取得できた銘柄数: {len(panel)}件")
                        for code in codes:
                            hist = trim_today(panel.get(code), st.session_state.exclude_today)
                            res = screen_hist(code, hist, **screen_kwargs)
                            if res:
                                screen_results.append(res)
                else:
                    progress_text = "銘柄データを解析中（データソース: yfinance）..."
                    my_bar = st.progress(0, text=progress_text)

                    def _screen_one(code):
                        hist = fetch_yfinance_history(code, from_str, to_str)
                        hist = trim_today(hist, st.session_state.exclude_today)
                        return screen_hist(code, hist, **screen_kwargs)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                        futures = {executor.submit(_screen_one, code): code for code in codes}
                        for i, future in enumerate(concurrent.futures.as_completed(futures)):
                            result = future.result()
                            if result:
                                screen_results.append(result)
                            my_bar.progress(
                                (i + 1) / len(codes),
                                text=f"{progress_text} ({i+1}/{len(codes)})",
                            )
                    my_bar.empty()

                m1, m2 = st.columns(2)
                m1.metric("① 対象銘柄数", f"{len(codes)} 件")
                m2.metric("② 条件クリア", f"{len(screen_results)} 件")

                final_results = []
                for res in screen_results:
                    code = res["code"]
                    match = target_df[target_df['コード_str'] == code]
                    if match.empty:
                        continue
                    row = match.iloc[0]
                    company_name = row['銘柄名']

                    final_results.append({
                        "コード": code,
                        "会社名": company_name,
                        "市場": row['市場・商品区分'],
                        "業種": row['33業種区分'],
                        "規模カテゴリ": row['規模カテゴリ'] if '規模カテゴリ' in row.index else None,
                        "平均出来高 (株)": int(round(res["avg_volume"])) if res["avg_volume"] is not None else "-",
                        "年初来安値更新": res.get("ytd_low_hit"),
                        "下落率 (%)": round(res["decline_pct"], 1) if res.get("decline_pct") is not None else "-",
                        "現在値 (円)": round(res["latest_close"], 1) if res.get("latest_close") is not None else "-",
                    })

                # PERは結果件数分だけyfinanceから補完取得する
                if final_results:
                    with st.spinner("PER（yfinance）を取得中..."):
                        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as per_executor:
                            per_futures = {
                                per_executor.submit(fetch_per_yfinance, r["コード"]): r["コード"]
                                for r in final_results
                            }
                            per_map = {}
                            for future in concurrent.futures.as_completed(per_futures):
                                code = per_futures[future]
                                per_map[code] = future.result()
                    for r in final_results:
                        per_val = per_map.get(r["コード"])
                        r["PER (倍)"] = round(per_val, 2) if per_val else "-"

                for res in final_results:
                    vol_text = f"{res['平均出来高 (株)']:,}株" if res['平均出来高 (株)'] != "-" else "-"
                    parts = [f"平均出来高: {vol_text}"]
                    if res["年初来安値更新"] is not None:
                        parts.append("年初来安値更新: 該当")
                    if res['下落率 (%)'] != "-":
                        parts.append(f"直近3ヶ月高値からの下落率: {res['下落率 (%)']}%")
                    if res['現在値 (円)'] != "-":
                        parts.append(f"現在値: {res['現在値 (円)']}円")
                    if res.get('PER (倍)', "-") != "-":
                        parts.append(f"PER: {res['PER (倍)']}倍")
                    msg = f"【スクリーニングヒット】\n{res['会社名']} ({res['コード']})\n" + " ｜ ".join(parts)
                    send_discord_notify(msg)

                st.session_state.last_screen_results = final_results
                st.session_state.last_screen_counts = (len(codes), len(screen_results))
                st.session_state.last_screen_conditions = (
                    st.session_state.use_ytd_low,
                    st.session_state.use_decline,
                    st.session_state.use_ore_teki,
                )

    # --- スクリーニング結果の表示（規模フィルター含む） ---
    if "last_screen_results" in st.session_state:
        final_results = st.session_state.last_screen_results
        total_count, hit_count = st.session_state.last_screen_counts

        if not search_btn:
            m1, m2 = st.columns(2)
            m1.metric("① 対象銘柄数", f"{total_count} 件")
            m2.metric("② 条件クリア", f"{hit_count} 件")

        st.markdown("---")

        if final_results:
            st.success(f"🎉 条件をクリアした銘柄が **{len(final_results)}件** 見つかりました！")

            has_size_category = any(r.get("規模カテゴリ") for r in final_results)
            display_results = final_results
            if has_size_category:
                size_label_map = {
                    "すべて": None,
                    "大型株（TOPIX100）": "大型株",
                    "中型株（TOPIX Mid400）": "中型株",
                    "小型株（TOPIX Small）": "小型株",
                }
                size_option = st.radio(
                    "規模区分で絞り込み",
                    list(size_label_map.keys()),
                    horizontal=True,
                    key="screen_size_filter",
                )
                target_size = size_label_map[size_option]
                if target_size:
                    display_results = [r for r in final_results if r.get("規模カテゴリ") == target_size]
                st.caption(f"表示件数: {len(display_results)}件")

            used_ytd_low, used_decline, used_ore_teki = st.session_state.get(
                "last_screen_conditions", (True, True, False)
            )

            sectors = sorted({r["業種"] for r in display_results if r.get("業種")})
            group_by_sector = st.checkbox("🏭 業種ごとにグループ表示する", value=True, key="screen_group_by_sector")

            def render_result_card(res):
                vol_text = f"{res['平均出来高 (株)']:,}株" if res['平均出来高 (株)'] != "-" else "-"
                caption_parts = [f"市場: {res['市場']}", f"業種: {res['業種']}"]
                if res.get("規模カテゴリ"):
                    caption_parts.append(f"規模: {res['規模カテゴリ']}")
                caption_parts.append(f"直近{LOOKBACK_DAYS}日平均出来高: {vol_text}")
                if used_ytd_low:
                    caption_parts.append("年初来安値更新: 該当")
                if (used_decline or used_ore_teki) and res.get('下落率 (%)', "-") != "-":
                    caption_parts.append(f"直近3ヶ月高値からの下落率: {res['下落率 (%)']}%")
                if used_ore_teki and res.get('現在値 (円)', "-") != "-":
                    caption_parts.append(f"現在値: {res['現在値 (円)']}円（俺的株）")
                if res.get('PER (倍)', "-") != "-":
                    caption_parts.append(f"PER: {res['PER (倍)']}倍")

                render_company_card(
                    res["会社名"],
                    res["コード"],
                    key_prefix="screen",
                    caption_parts=caption_parts,
                )

            if group_by_sector and sectors:
                for sector in sectors:
                    sector_results = [r for r in display_results if r.get("業種") == sector]
                    with st.expander(f"🏭 {sector}（{len(sector_results)}件）", expanded=True):
                        for res in sector_results:
                            render_result_card(res)
            else:
                for res in display_results:
                    render_result_card(res)
        else:
            st.warning("⚠️ 指定した条件をクリアした銘柄はありませんでした。")
    elif not search_btn:
        st.info("👆 上部のフィルターバーで条件を設定して「スクリーニングを実行する」ボタンを押してください。")

# ============================================================
# タブ2: 全銘柄一覧
# ============================================================
with tab_list:
    st.markdown("全銘柄の一覧です。銘柄コードを入力して検索するか、下のリストから確認してください。")
    st.markdown("---")

    if not df_jpx.empty:
        search_code_input = st.text_input("銘柄コードで検索（例: 4792, 7203）", value="")
        if search_code_input:
            code = search_code_input.strip()
            target_row = df_jpx[df_jpx['コード_str'] == code]
            if not target_row.empty:
                c_name = target_row.iloc[0]['銘柄名']

                render_company_card(
                    c_name,
                    code,
                    key_prefix="search",
                    caption_parts=[
                        f"市場: {target_row.iloc[0]['市場・商品区分']}",
                        f"業種: {target_row.iloc[0]['33業種区分']}",
                    ],
                )

                with st.container(border=True):
                    if st.session_state.data_source == "J-Quants" and not jquants_api_key:
                        st.info("J-Quantsを選択中の場合はサイドバーでAPIキーを入力すると出来高・年初来安値を確認できます。")
                    else:
                        with st.spinner("株価情報を取得中..."):
                            today = date.today()
                            jan1 = date(today.year, 1, 1)
                            from_s = jan1.strftime("%Y-%m-%d")
                            to_s = today.strftime("%Y-%m-%d")

                            if st.session_state.data_source == "J-Quants":
                                hist = fetch_jquants_single(code, from_s, to_s, jquants_api_key)
                            else:
                                hist = fetch_yfinance_history(code, from_s, to_s)
                            hist = trim_today(hist, st.session_state.exclude_today)

                            if hist is not None and not hist.empty:
                                ytd_low = hist['Low'].min()
                                latest_low = hist['Low'].iloc[-1]
                                is_ytd_low = latest_low <= ytd_low
                                recent_vol = hist['Volume'].tail(20)
                                avg_vol = recent_vol.mean() if len(recent_vol) > 0 else None

                                st.markdown(
                                    f"📉 **年初来安値:** {ytd_low:,.1f} 円 ｜ "
                                    f"**年初来安値更新:** {'✅ 更新中' if is_ytd_low else '－'}"
                                )
                                if avg_vol is not None and not pd.isna(avg_vol):
                                    st.markdown(f"📊 **直近20日間の平均出来高:** {int(round(avg_vol)):,}株")
                                st.caption(f"最終データ日: {hist.index[-1].date()}")

                                per_val = fetch_per_yfinance(code)
                                st.markdown(f"💰 **PER:** {round(per_val, 2) if per_val else '-'} 倍（yfinance）")
                            else:
                                st.markdown("📊 データを取得できませんでした。")
            else:
                st.error("指定されたコードが見つかりませんでした。")

        st.markdown("---")
        st.markdown("###### 🏷️ 規模別一覧（TOPIXの規模区分に基づく）")
        st.caption(
            "大型株：TOPIX100（Core30+Large70）対象の上位100銘柄 ｜ "
            "中型株：TOPIX Mid400対象の400銘柄 ｜ "
            "小型株：それ以外のTOPIX Small対象銘柄"
        )

        if '規模カテゴリ' in df_jpx.columns:
            size_label_map = {
                "大型株（TOPIX100）": "大型株",
                "中型株（TOPIX Mid400）": "中型株",
                "小型株（TOPIX Small）": "小型株",
            }
            size_option = st.radio(
                "規模区分を選択",
                list(size_label_map.keys()),
                horizontal=True,
            )
            size_df = df_jpx[df_jpx['規模カテゴリ'] == size_label_map[size_option]]

            list_search = st.text_input("銘柄名で絞り込み（任意）", value="", key="size_list_search")
            if list_search:
                size_df = size_df[size_df['銘柄名'].str.contains(list_search, na=False)]

            st.caption(f"該当銘柄数: {len(size_df)}件")

            display_df = size_df.head(50)
            if len(size_df) > 50:
                st.caption("※ 先頭50件を表示しています。銘柄名で絞り込むと目的の銘柄を見つけやすくなります。")

            show_per = st.checkbox(
                "💰 PERも表示する（yfinanceから取得・表示に少し時間がかかります）",
                value=False,
                key=f"sizelist_show_per_{size_label_map[size_option]}",
            )
            per_map = {}
            if show_per and not display_df.empty:
                with st.spinner("PERを取得中..."):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as per_executor:
                        codes_in_view = display_df['コード_str'].tolist()
                        per_futures = {
                            per_executor.submit(fetch_per_yfinance, c): c for c in codes_in_view
                        }
                        for future in concurrent.futures.as_completed(per_futures):
                            c = per_futures[future]
                            per_map[c] = future.result()

            for _, row in display_df.iterrows():
                code_str = row['コード_str']
                caption_parts = [
                    f"市場: {row['市場・商品区分']}",
                    f"業種: {row['33業種区分']}",
                ]
                if show_per:
                    per_val = per_map.get(code_str)
                    caption_parts.append(f"PER: {round(per_val, 2) if per_val else '-'} 倍")

                render_company_card(
                    row['銘柄名'],
                    code_str,
                    key_prefix=f"sizelist_{size_label_map[size_option]}",
                    caption_parts=caption_parts,
                )
        else:
            st.warning(
                "銘柄データに「規模区分」列が見つからなかったため、規模別一覧は表示できません。"
                " data_j.xlsが最新版（規模区分の列を含むもの）か確認してください。"
            )
    else:
        st.info("銘柄データが読み込まれていません。")
