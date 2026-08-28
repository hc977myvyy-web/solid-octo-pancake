import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures

# --- セッションステートの初期化 ---
if "layout_mode" not in st.session_state:
    st.session_state.layout_mode = "desktop"
if "market_filter" not in st.session_state:
    st.session_state.market_filter = "すべて"
if "sector_filter" not in st.session_state:
    st.session_state.sector_filter = "すべて"
if "size_filter" not in st.session_state:
    st.session_state.size_filter = "すべて"
if "use_ytd" not in st.session_state:
    st.session_state.use_ytd = True
if "use_range" not in st.session_state:
    st.session_state.use_range = True
if "min_range" not in st.session_state:
    st.session_state.min_range = 15.0
if "use_per" not in st.session_state:
    st.session_state.use_per = True
if "max_per" not in st.session_state:
    st.session_state.max_per = 15.0
if "use_roe" not in st.session_state:
    st.session_state.use_roe = True
if "min_roe" not in st.session_state:
    st.session_state.min_roe = 8.0
if "use_psr" not in st.session_state:
    st.session_state.use_psr = False
if "max_psr" not in st.session_state:
    st.session_state.max_psr = 5.0
if "use_yield" not in st.session_state:
    st.session_state.use_yield = False
if "min_yield" not in st.session_state:
    st.session_state.min_yield = 3.0

# --- ページ設定 ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide" if st.session_state.layout_mode == "desktop" else "centered",
)

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

def check_ytd_low(code, use_range, min_range_pct):
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(period="ytd")
        if len(hist) < 2:
            return None

        ytd_low = hist['Low'].min()
        ytd_high = hist['High'].max()
        latest_low = hist['Low'].iloc[-1]

        if use_range and min_range_pct > 0:
            if ytd_low <= 0:
                return None
            range_pct = (ytd_high - ytd_low) / ytd_low * 100
            if range_pct < min_range_pct:
                return None

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

# --- データ読み込み ---
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

# --- サイドバー：表示モードのみに簡素化 ---
st.sidebar.header("⚙️ 設定")
mode_label = st.sidebar.radio(
    "表示モード",
    ["🖥 デスクトップ", "📱 スマホ"],
    index=0 if st.session_state.layout_mode == "desktop" else 1,
    horizontal=True,
)
is_mobile = (mode_label != "🖥 デスクトップ") # 簡略判定
if ("desktop" if mode_label == "🖥 デスクトップ" else "mobile") != st.session_state.layout_mode:
    st.session_state.layout_mode = "desktop" if mode_label == "🖥 デスクトップ" else "mobile"
    st.rerun()

# --- メイン画面：TradingView風 上部フィルターパネル ---
st.title("📈 株式スクリーニングダッシュボード")
st.markdown("TradingView風のパネルから条件を選択し、スクリーニングを実行してください。")

with st.container(border=True):
    st.markdown("##### 🎛️ TradingView風 フィルターバー")
    
    # 1段目：基本セグメント（市場・業種・規模）
    f1, f2, f3 = st.columns(3)
    with f1:
        st.session_state.market_filter = st.selectbox("市場区分", market_options, index=market_options.index(st.session_state.market_filter) if st.session_state.market_filter in market_options else 0)
    with f2:
        st.session_state.sector_filter = st.selectbox("業種", sector_options, index=sector_options.index(st.session_state.sector_filter) if st.session_state.sector_filter in sector_options else 0)
    with f3:
        st.session_state.size_filter = st.selectbox("規模（時価総額）", size_options, index=size_options.index(st.session_state.size_filter) if st.session_state.size_filter in size_options else 0)

    st.markdown("---")
    
    # 2段目：価格＆テクニカル条件
    p1, p2, p3 = st.columns([1.2, 1, 1.8])
    with p1:
        st.session_state.use_ytd = st.checkbox("年初来安値更新", value=st.session_state.use_ytd)
    with p2:
        st.session_state.use_range = st.checkbox("レンジ下限絞り", value=st.session_state.use_range)
    with p3:
        st.session_state.min_range = st.number_input("レンジ下限(%)", min_value=0.0, value=st.session_state.min_range, step=1.0)

    # 3段目：財務指標フィルター
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        st.session_state.use_per = st.checkbox("PER制限", value=st.session_state.use_per)
        st.session_state.max_per = st.number_input("PER上限(倍)", min_value=0.0, value=st.session_state.max_per, step=1.0)
    with t2:
        st.session_state.use_roe = st.checkbox("ROE制限", value=st.session_state.use_roe)
        st.session_state.min_roe = st.number_input("ROE下限(%)", min_value=0.0, value=st.session_state.min_roe, step=1.0)
    with t3:
        st.session_state.use_psr = st.checkbox("PSR制限", value=st.session_state.use_psr)
        st.session_state.max_psr = st.number_input("PSR上限(倍)", min_value=0.0, value=st.session_state.max_psr, step=1.0)
    with t4:
        st.session_state.use_yield = st.checkbox("配当利回り制限", value=st.session_state.use_yield)
        st.session_state.min_yield = st.number_input("利回り下限(%)", min_value=0.0, value=st.session_state.min_yield, step=1.0)

    st.markdown("")
    search_btn = st.button("🚀 スクリーニングを実行する", type="primary", use_container_width=True)

st.markdown("---")
tab_screen, tab_list = st.tabs(["🔍 スクリーニング結果", "📋 全銘柄一覧（規模別）"])

# ============================================================
# タブ1: スクリーニング実行
# ============================================================
with tab_screen:
    if search_btn and not df_jpx.empty:
        target_df = df_jpx.copy()
        
        if st.session_state.market_filter != "すべて":
            target_df = target_df[target_df['市場・商品区分'] == st.session_state.market_filter]
        if st.session_state.sector_filter != "すべて":
            target_df = target_df[target_df['33業種区分'] == st.session_state.sector_filter]
        if st.session_state.size_filter != "すべて":
            target_df = target_df[target_df['規模'] == st.session_state.size_filter]
            
        codes = target_df['コード'].astype(str).tolist()

        if len(codes) == 0:
            st.warning("⚠️ 条件に合致する銘柄がありませんでした。")
        else:
            if st.session_state.use_ytd:
                progress_text = "株価データ＆条件を解析中..."
                my_bar = st.progress(0, text=progress_text)
                ytd_low_codes = []

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {
                        executor.submit(
                            check_ytd_low,
                            code,
                            st.session_state.use_range,
                            st.session_state.min_range
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

            # サマリーメトリクス
            m1, m2 = st.columns(2)
            m1.metric("① 対象銘柄数", f"{len(codes)} 件")
            m2.metric("② 価格条件クリア", f"{len(ytd_low_codes)} 件")

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
                        
                        if st.session_state.use_per:
                            per_ok = (data['PER'] is not None) and (data['PER'] <= st.session_state.max_per)
                        if st.session_state.use_roe:
                            roe_ok = (data['ROE'] is not None) and (data['ROE'] >= st.session_state.min_roe)
                        if st.session_state.use_psr:
                            psr_ok = (data['PSR'] is not None) and (data['PSR'] <= st.session_state.max_psr)
                        if st.session_state.use_yield:
                            yield_ok = (data['Yield'] is not None) and (data['Yield'] >= st.session_state.min_yield)
                            
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
                
                st.markdown("---")
                if final_results:
                    st.success(f"🎉 条件をクリアしたお宝候補が **{len(final_results)}件** 見つかりました！")
                    result_df = pd.DataFrame(final_results)

                    column_config = {
                        "銘柄 name": st.column_config.LinkColumn(
                            "銘柄名 (クリックでTradingViewへ)",
                            help="クリックしてチャート・財務を確認",
                            display_text=r".*#(.+)"
                        ),
                        "銘柄名": st.column_config.LinkColumn(
                            "銘柄名 (クリックでTradingViewへ)",
                            help="クリックしてチャート・財務を確認",
                            display_text=r".*#(.+)"
                        ),
                        "会社名": None
                    }
                    
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
                    st.warning("⚠️ 指定した財務条件をすべてクリアした銘柄はありませんでした。")
            else:
                st.warning("⚠️ 価格条件に合致する銘柄はありませんでした。")
    elif not search_btn:
        st.info("👆 上部のパネルで条件を設定して「スクリーニングを実行する」ボタンを押してください。")

# ============================================================
# タブ2: 銘柄一覧（規模別）
# ============================================================
with tab_list:
    st.markdown("規模区分ごとの全銘柄一覧です。銘柄名をクリックするとTradingViewが開きます。")
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
        st.info("銘柄データが読み込まれていません。")
