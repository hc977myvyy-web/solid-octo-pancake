import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures

# --- 表示モード（デスクトップ／スマホ）をセッションに保持 ---
if "layout_mode" not in st.session_state:
    st.session_state.layout_mode = "desktop"

# --- サイドバーの開閉状態をセッションに保持 ---
if "sidebar_state" not in st.session_state:
    st.session_state.sidebar_state = "expanded"

# --- ページ設定 ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide" if st.session_state.layout_mode == "desktop" else "centered",
    initial_sidebar_state=st.session_state.sidebar_state,
)

if st.session_state.sidebar_state == "collapsed":
    st.session_state.sidebar_state = "auto"

# --- 設定 ---
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
        st.error(f"銘柄データの取得に失敗しました: {e}")
        return pd.DataFrame()

def get_market_cap(code):
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        cap = info.get('marketCap')
        return {"code": code, "market_cap": cap}
    except:
        return {"code": code, "market_cap": None}

@st.cache_data(ttl=86400)
def load_market_caps(codes):
    results = []
    progress_text = "時価総額データを取得中（初回は数分かかることがあります）..."
    my_bar = st.progress(0, text=progress_text)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_market_cap, code): code for code in codes}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            results.append(future.result())
            my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")

    my_bar.empty()

    cap_df = pd.DataFrame(results)
    ranked = cap_df.dropna(subset=['market_cap']).sort_values('market_cap', ascending=False).reset_index(drop=True)

    def classify_size(rank):
        if rank < 100:
            return "大型株"
        elif rank < 500:
            return "中型株"
        else:
            return "小型株"

    ranked['規模'] = [classify_size(i) for i in range(len(ranked))]
    cap_df = cap_df.merge(ranked[['code', '規模']], on='code', how='left')
    cap_df['規模'] = cap_df['規模'].fillna("小型株")

    return cap_df[['code', '規模']]

def check_ytd_low(code, min_recent5_avg_volume, max_zero_volume_ratio, min_range_pct):
    """
    年初来安値の更新判定 ＋ 閑散銘柄除外 ＋ 値動き率（レンジ）フィルター
    """
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(period="ytd")
        if len(hist) < 2:
            return None

        # 1. 出来高フィルター（直近5日平均）
        recent5 = hist['Volume'].tail(5)
        recent5_avg_volume = recent5.mean()
        if pd.isna(recent5_avg_volume) or recent5_avg_volume < min_recent5_avg_volume:
            return None

        # 2. 出来高ゼロ日の許容割合
        zero_volume_days = (hist['Volume'] == 0).sum()
        zero_volume_ratio = zero_volume_days / len(hist)
        if zero_volume_ratio > max_zero_volume_ratio:
            return None

        ytd_low = hist['Low'].min()
        ytd_high = hist['High'].max()
        latest_low = hist['Low'].iloc[-1]

        # 3. 値動き率（レンジ）フィルター：(高値 - 安値) ÷ 安値 ≧ 指定％
        if min_range_pct > 0:
            if ytd_low <= 0:
                return None
            range_pct = (ytd_high - ytd_low) / ytd_low * 100
            if range_pct < min_range_pct:
                return None

        # 4. 年初来安値の更新判定
        if latest_low <= ytd_low:
            return code
    except:
        pass
    return None

def get_fundamentals(code):
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        per = info.get('trailingPE') or info.get('forwardPE')
        roe = info.get('returnOnEquity')
        psr = info.get('priceToSalesTrailing12Months')
        dividend_yield = info.get('dividendYield')
        
        if roe is not None:
            roe = roe * 100 
        if dividend_yield is not None:
            dividend_yield = dividend_yield * 100
            
        return {'code': code, 'PER': per, 'ROE': roe, 'PSR': psr, 'Yield': dividend_yield}
    except:
        return {'code': code, 'PER': None, 'ROE': None, 'PSR': None, 'Yield': None}

def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})

def make_tradingview_link_df(sub_df, is_mobile):
    display_rows = []
    for _, row in sub_df.iterrows():
        tv_url = f"https://www.tradingview.com/symbols/TSE-{row['コード']}/#{row['銘柄名']}"
        entry = {
            "銘柄名": tv_url,
            "コード": row['コード'],
        }
        if not is_mobile:
            entry["市場"] = row['市場・商品区分']
            entry["業種"] = row['33業種区分']
        display_rows.append(entry)
    return pd.DataFrame(display_rows)

def render_link_table(display_df):
    st.dataframe(
        display_df,
        column_config={
            "銘柄名": st.column_config.LinkColumn(
                "銘柄名 (クリックでTradingViewへ)",
                help="クリックしてチャート・財務を確認",
                display_text=r".*#(.+)"
            ),
        },
        use_container_width=True,
        hide_index=True
    )

def render_condition(label, number_label, default_check, default_value, is_mobile, key_prefix):
    if is_mobile:
        use = st.checkbox(label, value=default_check, key=f"{key_prefix}_chk")
        val = st.number_input(number_label, min_value=0.0, value=default_value, key=f"{key_prefix}_val")
    else:
        c1, c2 = st.columns([1, 2])
        use = c1.checkbox(label, value=default_check, key=f"{key_prefix}_chk")
        val = c2.number_input(number_label, min_value=0.0, value=default_value, label_visibility="collapsed", key=f"{key_prefix}_val")
    return use, val

# --- サイドバー：表示モード切り替え ---
st.sidebar.header("🔍 検索フィルター")

mode_label = st.sidebar.radio(
    "表示モード",
    ["🖥 デスクトップ", "📱 スマホ"],
    index=0 if st.session_state.layout_mode == "desktop" else 1,
    horizontal=True,
)
new_mode = "desktop" if mode_label == "🖥 デスクトップ" else "mobile"
if new_mode != st.session_state.layout_mode:
    st.session_state.layout_mode = new_mode
    st.rerun()

is_mobile = st.session_state.layout_mode == "mobile"
st.sidebar.markdown("---")

df_jpx = load_jpx_data()

if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
    codes_all = tuple(df_jpx['コード_str'].tolist())
    size_df = load_market_caps(codes_all)
    df_jpx = df_jpx.merge(size_df, left_on='コード_str', right_on='code', how='left')
    df_jpx['規模'] = df_jpx['規模'].fillna("小型株")

    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())
    size_options = ["すべて", "大型株", "中型株", "小型株"]

    with st.sidebar.expander("① 対象銘柄の条件", expanded=True):
        selected_market = st.selectbox("市場区分", market_options, index=0)
        selected_sector = st.selectbox("業種", sector_options, index=0)
        selected_size = st.selectbox("規模（時価総額ベース）", size_options, index=0)

    with st.sidebar.expander("② 価格・値動き率フィルター", expanded=True):
        use_ytd_low = st.checkbox("年初来安値更新銘柄に絞る", value=True)
        st.caption("山田コンサル並みの値動き・出来高条件を一括適用")
        min_recent5_avg_volume = st.number_input(
            "直近5日平均出来高の下限(株)", min_value=0, value=1000, step=500,
            help="直近5営業日の平均出来高がこれ未満の銘柄は除外します"
        )
        max_zero_volume_ratio_pct = st.slider(
            "年初来の出来高ゼロ日の許容割合(%)", min_value=0, max_value=100, value=30, step=5,
            help="年初来データのうち出来高ゼロだった日の割合がこれを超える銘柄は除外します"
        )
        # ★ここを山田コンサル水準（例: 15.0%）に合わせやすく初期値を設定
        min_range_pct = st.number_input(
            "年初来レンジ(高値-安値)の下限(%)", min_value=0.0, value=15.0, step=1.0,
            help="山田コンサル並みなら15.0%以上に設定すると、年間を通じて一定の値幅がある銘柄だけを狙えます"
        )

    with st.sidebar.expander("③ 財務条件", expanded=True):
        use_per, max_per = render_condition("PER", "PER上限(倍)", True, 15.0, is_mobile, "per")
        use_roe, min_roe = render_condition("ROE", "ROE下限(%)", True, 8.0, is_mobile, "roe")
        use_psr, max_psr = render_condition("PSR", "PSR上限(倍)", False, 5.0, is_mobile, "psr")
        use_yield, min_yield = render_condition("利回り", "利回り下限(%)", False, 3.0, is_mobile, "yield")
    
    st.sidebar.markdown("---")
    search_btn = st.sidebar.button("🚀 スクリーニング開始", type="primary", use_container_width=True)

    if search_btn and is_mobile:
        st.session_state.sidebar_state = "collapsed"
        st.rerun()

# --- メイン画面（タブ構成） ---
st.title("📈 年初来安値 ＆ 財務スクリーニングダッシュボード")

tab_screen, tab_list = st.tabs(["🔍 スクリーニング", "📋 銘柄一覧（規模別）"])

# ============================================================
# タブ1: スクリーニング
# ============================================================
with tab_screen:
    st.markdown("条件に合致するお宝銘柄をリアルタイムで抽出します。銘柄名をクリックするとTradingViewが開きます。")
    st.markdown("---")

    if search_btn and not df_jpx.empty:
        target_df = df_jpx.copy()
        
        if selected_market != "すべて":
            target_df = target_df[target_df['市場・商品区分'] == selected_market]
        if selected_sector != "すべて":
            target_df = target_df[target_df['33業種区分'] == selected_sector]
        if selected_size != "すべて":
            target_df = target_df[target_df['規模'] == selected_size]
            
        codes = target_df['コード'].astype(str).tolist()

        if len(codes) == 0:
            st.warning("⚠️ 条件に合致する銘柄がありませんでした。市場・業種・規模の条件を見直してください。")
        else:
            if use_ytd_low:
                progress_text = "株価データ＆値動き率フィルターをチェック中..."
                my_bar = st.progress(0, text=progress_text)
                ytd_low_codes = []
                max_zero_volume_ratio = max_zero_volume_ratio_pct / 100.0

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {
                        executor.submit(
                            check_ytd_low,
                            code,
                            min_recent5_avg_volume,
                            max_zero_volume_ratio,
                            min_range_pct
                        ): code
                        for code in codes
                    }
                    for i, future in enumerate(concurrent.futures.as_completed(futures)):
                        result = future.result()
                        if result:
                            ytd_low_codes.append(result)
                        my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")
                
                my_bar.empty()
            else:
                ytd_low_codes = codes

            with st.container(border=True):
                if is_mobile:
                    st.metric("① 対象銘柄数", f"{len(codes)} 件")
                    st.metric("② 安値＆値動き率クリア", f"{len(ytd_low_codes)} 件" if use_ytd_low else "条件なし（全銘柄通過）")
                else:
                    c1, c2 = st.columns(2)
                    c1.metric("① 対象銘柄数", f"{len(codes)} 件")
                    c2.metric("② 安値＆値動き率クリア", f"{len(ytd_low_codes)} 件" if use_ytd_low else "条件なし（全銘柄通過）")

            if len(ytd_low_codes) > 0:
                final_results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(get_fundamentals, code): code for code in ytd_low_codes}
                    for future in concurrent.futures.as_completed(futures):
                        data = future.result()
                        
                        per_ok = True
                        roe_ok = True
                        psr_ok = True
                        yield_ok = True
                        
                        if use_per:
                            per_ok = (data['PER'] is not None) and (data['PER'] <= max_per)
                        if use_roe:
                            roe_ok = (data['ROE'] is not None) and (data['ROE'] >= min_roe)
                        if use_psr:
                            psr_ok = (data['PSR'] is not None) and (data['PSR'] <= max_psr)
                        if use_yield:
                            yield_ok = (data['Yield'] is not None) and (data['Yield'] >= min_yield)
                            
                        if per_ok and roe_ok and psr_ok and yield_ok:
                            row = target_df[target_df['コード'].astype(str) == data['code']].iloc[0]
                            company_name = row['銘柄名']
                            tv_url = f"https://www.tradingview.com/symbols/TSE-{data['code']}/#{company_name}"
                            
                            final_results.append({
                                "コード": data['code'],
                                "銘柄名": tv_url,
                                "会社名": company_name,
                                "市場": row['市場・商品区分'],
                                "業種": row['33業種区分'],
                                "規模": row['規模'],
                                "PER (倍)": round(data['PER'], 2) if data['PER'] else "-",
                                "ROE (%)": round(data['ROE'], 2) if data['ROE'] else "-",
                                "PSR (倍)": round(data['PSR'], 2) if data['PSR'] else "-",
                                "配当利回り (%)": round(data['Yield'], 2) if data['Yield'] else "-",
                            })
                
                st.metric("🏆 最終条件クリア銘柄", f"{len(final_results)} 件")
                st.markdown("---")
                
                if final_results:
                    st.success(f"🎉 条件をクリアしたお宝候補が **{len(final_results)}件** 見つかりました！")
                    result_df = pd.DataFrame(final_results)

                    column_config = {
                        "銘柄名": st.column_config.LinkColumn(
                            "銘柄名 (クリックでTradingViewへ)",
                            help="クリックしてチャート・財務を確認",
                            display_text=r".*#(.+)"
                        ),
                        "会社名": None
                    }
                    if is_mobile:
                        column_config["市場"] = None
                        column_config["業種"] = None
                    
                    st.dataframe(
                        result_df,
                        column_config=column_config,
                        use_container_width=True,
                        hide_index=True
                    )
                    
                    for res in final_results:
                        msg = f"【安値更新＆条件クリア】\n{res['会社名']} ({res['コード']})\n規模: {res['規模']}\nPER: {res['PER (倍)']} / ROE: {res['ROE (%)']}% / PSR: {res['PSR (倍)']} / 配当利回り: {res['配当利回り (%)']}%"
                        send_discord_notify(msg)
                else:
                    st.warning("⚠️ 指定した条件をすべてクリアした銘柄はありませんでした（条件を少し緩めると見つかる場合があります）。")
            else:
                st.warning("⚠️ 選択した市場・業種・規模の中で、条件に合致する銘柄はありませんでした。")
    elif not search_btn:
        st.info("👈 サイドバーで条件を設定して「スクリーニング開始」ボタンを押してください。")

# ============================================================
# タブ2: 銘柄一覧（規模別）
# ============================================================
with tab_list:
    st.markdown("検索条件に関わらず、**規模区分（大型株・中型株・小型株）ごとの全銘柄一覧**をいつでも確認できます。銘柄名をクリックするとTradingViewが開きます。")
    st.markdown("---")

    if not df_jpx.empty:
        size_tab_large, size_tab_mid, size_tab_small = st.tabs([
            f"🔵 大型株（{len(df_jpx[df_jpx['規模'] == '大型株'])}件）",
            f"🟢 中型株（{len(df_jpx[df_jpx['規模'] == '中型株'])}件）",
            f"⚪ 小型株（{len(df_jpx[df_jpx['規模'] == '小型株'])}件）",
        ])

        with size_tab_large:
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '大型株'], is_mobile))

        with size_tab_mid:
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '中型株'], is_mobile))

        with size_tab_small:
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '小型株'], is_mobile))
    else:
        st.info("銘柄データが読み込まれていません。data_j.xls を確認してください。")
