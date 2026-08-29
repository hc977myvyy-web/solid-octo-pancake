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

# --- スクリーニング条件（固定値） ---
LOOKBACK_DAYS = 20
MIN_AVG_VOLUME = 10000

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
        if '規模区分' in df.columns:
            df['規模カテゴリ'] = df['規模区分'].apply(classify_size)
        return df
    except Exception as e:
        st.error(f"銘柄データの取得に失敗しました: data_j.xls ファイルを確認してください: {e}")
        return pd.DataFrame()


# ============================================================
# データ取得レイヤー（yfinance / J-Quants を切り替え可能にする）
# ============================================================

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_yfinance_history(code, from_date_str, to_date_str):
    """yfinanceから日足データ（High, Low, Volume）を取得する"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(start=from_date_str, end=to_date_str)
        if hist.empty:
            return pd.DataFrame()
        hist = hist.rename(columns={"High": "High", "Low": "Low", "Volume": "Volume"})
        return hist[["High", "Low", "Volume"]]
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_jquants_history(code, from_date_str, to_date_str, api_key):
    """
    J-Quants API v2 (/v2/equities/bars/daily) から日足データを取得する。
    認証はダッシュボードで発行したAPIキーを x-api-key ヘッダーに付与する方式。
    pagination_key が返る場合は続きのページを取得する。
    """
    if not api_key:
        return pd.DataFrame()

    url = f"{JQUANTS_BASE_URL}/equities/bars/daily"
    headers = {"x-api-key": api_key}
    params = {"code": code, "from": from_date_str, "to": to_date_str}

    records = []
    pagination_key = None
    try:
        while True:
            if pagination_key:
                params["pagination_key"] = pagination_key
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code != 200:
                # 認証エラー・レートリミット等はここで打ち切る
                break
            payload = res.json()
            records.extend(payload.get("data", []))
            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                break
    except Exception:
        pass

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if df.empty or "Date" not in df.columns:
        return pd.DataFrame()

    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")

    # 調整済み高値・安値・出来高（AdjH/AdjL/AdjVo）を使用。取引が無い日はNULLなので除外。
    df = df.rename(columns={"AdjH": "High", "AdjL": "Low", "AdjVo": "Volume"})
    keep_cols = [c for c in ["High", "Low", "Volume"] if c in df.columns]
    df = df[keep_cols].dropna(how="all")
    return df


def get_price_history(code, from_dt, to_dt, source, api_key=None):
    from_str = from_dt.strftime("%Y-%m-%d")
    to_str = to_dt.strftime("%Y-%m-%d")
    if source == "J-Quants":
        return fetch_jquants_history(code, from_str, to_str, api_key)
    else:
        return fetch_yfinance_history(code, from_str, to_str)


# ============================================================
# スクリーニング条件の判定
# ============================================================

def screen_code(code, min_avg_volume, lookback_days, source, api_key=None):
    """
    1銘柄に対して「年初来安値更新」かつ「直近N日平均出来高が下限以上」を判定する。
    出来高フィルターは市場区分・業種で絞り込んだ銘柄を対象に、
    年初来安値をスクリーニングするための足切り条件として常に適用する。
    両方を満たした場合のみ結果を返す。
    """
    today = date.today()
    jan1 = date(today.year, 1, 1)
    # 出来高判定用に土日・祝日を考慮して少し多めに取得する
    volume_from = today - timedelta(days=int(lookback_days * 2.5) + 10)
    from_dt = min(jan1, volume_from)

    hist = get_price_history(code, from_dt, today, source, api_key)
    if hist is None or hist.empty:
        return None

    # 1. 出来高条件（足切り）
    if len(hist) < lookback_days:
        return None
    recent_vol = hist['Volume'].tail(lookback_days)
    avg_volume = recent_vol.mean()
    if pd.isna(avg_volume) or avg_volume < min_avg_volume:
        return None

    # 2. 年初来安値更新の条件
    ytd_hist = hist[hist.index.date >= jan1]
    if len(ytd_hist) < 2:
        return None
    ytd_low = ytd_hist['Low'].min()
    latest_low = ytd_hist['Low'].iloc[-1]
    if latest_low > ytd_low:
        return None

    return {"code": code, "avg_volume": avg_volume}


def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})


def tradingview_financials_url(code):
    """TradingViewの財務情報タブのURL（TradingViewアプリが入っている端末ではアプリが自動的に開く）"""
    return f"https://www.tradingview.com/symbols/TSE-{code}/financials-overview/"


GEMINI_URL = "https://gemini.google.com/app"


def build_fundamental_prompt(company_name, code):
    return (
        f"{company_name}（証券コード: {code}、東京証券取引所上場）について、"
        f"直近の業績・財務状況をふまえたファンダメンタル分析をお願いします。\n"
        f"・強み、割安感、注目ポイントを3行程度で\n"
        f"・年初来安値を更新し、出来高が増えている背景として考えられる要因も教えてください"
    )


def render_company_card(company_name, code, key_prefix, caption_parts=None):
    """
    企業名（TradingView財務ページへのリンク）＋Geminiでのファンダメンタル分析導線をまとめたカードを表示する。
    key_prefix: 同じコードの銘柄が複数箇所（スクリーニング結果・規模別一覧など）に出ても
                ウィジェットIDが衝突しないようにするための接頭辞。
    """
    tv_url = tradingview_financials_url(code)
    with st.container(border=True):
        st.markdown(
            f"#### [{company_name}]({tv_url}) "
            f"<span style='font-size:0.8em; color:gray;'>({code})</span>",
            unsafe_allow_html=True,
        )
        if caption_parts:
            st.caption(" ｜ ".join(caption_parts))
        st.caption("👆 企業名をタップするとTradingViewの財務情報ページを開きます（アプリがあれば自動的にアプリが開きます）")

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
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
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

st.sidebar.caption(
    "※ J-QuantsのFreeプランはデータに遅延があります。プランによって取得できる期間や項目が異なります。"
)

# --- メイン画面：フィルターバー ---
st.title("📈 株式スクリーニングダッシュボード")
st.markdown("条件を設定してスクリーニングを実行するか、全銘柄一覧タブをご確認ください。")

with st.container(border=True):
    st.markdown("##### 🎛️ フィルターバー")

    # 1. 業種・市場区分フィルター
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

    # 2. スクリーニング条件（固定）：出来高（足切り条件） × 年初来安値更新
    st.markdown("###### 📉 スクリーニング条件（年初来安値更新）")
    st.caption(
        f"市場区分・業種で絞り込んだ銘柄のうち、直近{LOOKBACK_DAYS}日間の平均出来高が"
        f"{MIN_AVG_VOLUME:,}株以上の銘柄を対象に、"
        "「当日の安値が年初来安値を更新」しているかをスクリーニングします（条件は固定です）。"
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
        else:
            target_df = df_jpx.copy()

            if st.session_state.market_filter != "すべて":
                target_df = target_df[target_df['市場・商品区分'] == st.session_state.market_filter]
            if st.session_state.sector_filter != "すべて":
                target_df = target_df[target_df['33業種区分'] == st.session_state.sector_filter]

            codes = target_df['コード'].astype(str).tolist()

            if len(codes) == 0:
                st.warning("⚠️ 条件に合致する銘柄がありませんでした。")
            else:
                progress_text = f"銘柄データを解析中（データソース: {st.session_state.data_source}）..."
                my_bar = st.progress(0, text=progress_text)
                screen_results = []

                # J-Quantsはレートリミットがあるため同時実行数を抑える
                max_workers = 3 if st.session_state.data_source == "J-Quants" else 6

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            screen_code,
                            code,
                            MIN_AVG_VOLUME,
                            LOOKBACK_DAYS,
                            st.session_state.data_source,
                            jquants_api_key,
                        ): code
                        for code in codes
                    }
                    for i, future in enumerate(concurrent.futures.as_completed(futures)):
                        result = future.result()
                        if result:
                            screen_results.append(result)
                        my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")
                my_bar.empty()

                m1, m2 = st.columns(2)
                m1.metric("① 対象銘柄数", f"{len(codes)} 件")
                m2.metric("② 条件クリア", f"{len(screen_results)} 件")

                st.markdown("---")
                if screen_results:
                    st.success(f"🎉 条件をクリアした銘柄が **{len(screen_results)}件** 見つかりました！")

                    final_results = []
                    for res in screen_results:
                        code = res["code"]
                        row = target_df[target_df['コード'].astype(str) == code].iloc[0]
                        company_name = row['銘柄名']

                        final_results.append({
                            "コード": code,
                            "会社名": company_name,
                            "市場": row['市場・商品区分'],
                            "業種": row['33業種区分'],
                            "平均出来高 (株)": int(round(res["avg_volume"])) if res["avg_volume"] is not None else "-",
                        })

                    for res in final_results:
                        vol_text = f"{res['平均出来高 (株)']:,}株" if res['平均出来高 (株)'] != "-" else "-"

                        render_company_card(
                            res["会社名"],
                            res["コード"],
                            key_prefix="screen",
                            caption_parts=[
                                f"市場: {res['市場']}",
                                f"業種: {res['業種']}",
                                f"直近{LOOKBACK_DAYS}日平均出来高: {vol_text}",
                            ],
                        )

                        msg = f"【スクリーニングヒット】\n{res['会社名']} ({res['コード']})\n平均出来高: {vol_text}"
                        send_discord_notify(msg)
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
            target_row = df_jpx[df_jpx['コード'].astype(str) == search_code_input.strip()]
            if not target_row.empty:
                c_name = target_row.iloc[0]['銘柄名']
                code = search_code_input.strip()

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
                            hist = get_price_history(
                                code, jan1, today, st.session_state.data_source, jquants_api_key
                            )
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

            for _, row in display_df.iterrows():
                render_company_card(
                    row['銘柄名'],
                    str(row['コード']),
                    key_prefix=f"sizelist_{size_label_map[size_option]}",
                    caption_parts=[
                        f"市場: {row['市場・商品区分']}",
                        f"業種: {row['33業種区分']}",
                    ],
                )
        else:
            st.warning(
                "銘柄データに「規模区分」列が見つからなかったため、規模別一覧は表示できません。"
                " data_j.xlsが最新版（規模区分の列を含むもの）か確認してください。"
            )
    else:
        st.info("銘柄データが読み込まれていません。")
