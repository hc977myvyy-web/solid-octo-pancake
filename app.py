import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures

# --- セッションステートの初期化 ---
if "market_filter" not in st.session_state:
    st.session_state.market_filter = "すべて"
if "sector_filter" not in st.session_state:
    st.session_state.sector_filter = "すべて"
if "min_avg_volume" not in st.session_state:
    st.session_state.min_avg_volume = 10000

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


@st.cache_data(ttl=86400)
def load_jpx_data():
    try:
        df = pd.read_excel("data_j.xls")
        df = df[df['市場・商品区分'].notna()]
        return df
    except Exception as e:
        st.error(f"銘柄データの取得に失敗しました: data_j.xls ファイルを確認してください: {e}")
        return pd.DataFrame()


def check_avg_volume(code, min_avg_volume, lookback_days=20):
    """
    直近 lookback_days 営業日の平均出来高が min_avg_volume 以上かどうかを判定する。
    条件を満たせば (code, avg_volume) を返し、満たさなければ None を返す。

    ※ 現在は yfinance を利用。J-Quants API に切り替える場合は、
      この関数の中身を J-Quants の /prices/daily_quotes (または
      v2の /equities/bars/daily) から出来高を取得する処理に差し替える。
    """
    try:
        ticker = yf.Ticker(f"{code}.T")
        # 土日祝日や欠損を考慮し、少し多めに1ヶ月分を取得してから直近N日分を使う
        hist = ticker.history(period="1mo")
        if len(hist) < lookback_days:
            return None

        recent = hist['Volume'].tail(lookback_days)
        avg_volume = recent.mean()

        if avg_volume >= min_avg_volume:
            return (code, avg_volume)
    except Exception:
        pass
    return None


def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})


# --- データ読み込み ---
df_jpx = load_jpx_data()
if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())

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

    # 2. 出来高フィルター（例：山田コンサルティンググループの直近20日平均出来高が1万株以上）
    st.markdown("###### 📊 出来高フィルター")
    v1, v2 = st.columns([1, 2])
    with v1:
        lookback_days = st.number_input("集計日数（営業日）", min_value=5, max_value=60, value=20, step=1)
    with v2:
        st.session_state.min_avg_volume = st.number_input(
            "平均出来高の下限 (株)",
            min_value=0,
            value=st.session_state.min_avg_volume,
            step=1000,
            help="例：山田コンサルティンググループ(4792)のように、直近20日間の平均出来高が1万株以上の銘柄に絞り込みます",
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
        target_df = df_jpx.copy()

        if st.session_state.market_filter != "すべて":
            target_df = target_df[target_df['市場・商品区分'] == st.session_state.market_filter]
        if st.session_state.sector_filter != "すべて":
            target_df = target_df[target_df['33業種区分'] == st.session_state.sector_filter]

        codes = target_df['コード'].astype(str).tolist()

        if len(codes) == 0:
            st.warning("⚠️ 条件に合致する銘柄がありませんでした。")
        else:
            progress_text = f"直近{lookback_days}日間の平均出来高を解析中..."
            my_bar = st.progress(0, text=progress_text)
            volume_results = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(
                        check_avg_volume,
                        code,
                        st.session_state.min_avg_volume,
                        lookback_days,
                    ): code
                    for code in codes
                }
                for i, future in enumerate(concurrent.futures.as_completed(futures)):
                    result = future.result()
                    if result:
                        volume_results.append(result)
                    my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")
            my_bar.empty()

            m1, m2 = st.columns(2)
            m1.metric("① 対象銘柄数", f"{len(codes)} 件")
            m2.metric("② 出来高条件クリア", f"{len(volume_results)} 件")

            st.markdown("---")
            if volume_results:
                st.success(f"🎉 条件をクリアした銘柄が **{len(volume_results)}件** 見つかりました！")

                final_results = []
                for code, avg_volume in volume_results:
                    row = target_df[target_df['コード'].astype(str) == code].iloc[0]
                    company_name = row['銘柄名']
                    tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{company_name}"

                    final_results.append({
                        "コード": code,
                        "銘柄名": tv_url,
                        "会社名": company_name,
                        "市場": row['市場・商品区分'],
                        "業種": row['33業種区分'],
                        "平均出来高 (株)": int(round(avg_volume)),
                    })

                result_df = pd.DataFrame(final_results).sort_values("平均出来高 (株)", ascending=False)

                column_config = {
                    "銘柄名": st.column_config.LinkColumn(
                        "銘柄名 (クリックでTradingViewへ)",
                        help="クリックしてチャートを確認",
                        display_text=r".*#(.+)"
                    ),
                    "会社名": None,
                }

                st.dataframe(
                    result_df,
                    column_config=column_config,
                    use_container_width=True,
                    hide_index=True,
                )

                for res in final_results:
                    msg = f"【スクリーニングヒット】\n{res['会社名']} ({res['コード']})\n平均出来高: {res['平均出来高 (株)']:,}株"
                    send_discord_notify(msg)
            else:
                st.warning("⚠️ 指定した出来高条件をクリアした銘柄はありませんでした。")
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
                tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{c_name}"

                with st.container(border=True):
                    st.markdown(
                        f"**[{c_name}]({tv_url})** （コード: `{code}`） / "
                        f"市場: {target_row.iloc[0]['市場・商品区分']} / 業種: {target_row.iloc[0]['33業種区分']}"
                    )
                    with st.spinner("出来高を取得中..."):
                        result = check_avg_volume(code, 0, lookback_days=20)
                        if result:
                            _, avg_vol = result
                            st.markdown(f"📊 **直近20日間の平均出来高:** {int(round(avg_vol)):,}株")
                        else:
                            st.markdown("📊 出来高データを取得できませんでした。")
            else:
                st.error("指定されたコードが見つかりませんでした。")

        st.markdown("---")
        st.caption("全銘柄リスト（最初の50件を表示）")
        sample_df = df_jpx.head(50)
        for _, row in sample_df.iterrows():
            code = row['コード']
            name = row['銘柄名']
            tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{name}"
            st.markdown(f"- **[{name}]({tv_url})** (`{code}`) - 市場: {row['市場・商品区分']} / 業種: {row['33業種区分']}")
    else:
        st.info("銘柄データが読み込まれていません。")
