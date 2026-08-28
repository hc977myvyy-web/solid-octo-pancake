import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures

# --- ページ設定（最初から画面を広く使うワイドモードに設定） ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide"
)

# --- 設定 ---
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
    """時価総額を取得（規模区分の判定用）"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        cap = info.get('marketCap')
        return {"code": code, "market_cap": cap}
    except:
        return {"code": code, "market_cap": None}

@st.cache_data(ttl=86400)
def load_market_caps(codes):
    """全銘柄の時価総額を並列取得し、規模区分（大型/中型/小型）を付与して返す"""
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
    # 時価総額が取得できた銘柄だけを大きい順に並べる
    ranked = cap_df.dropna(subset=['market_cap']).sort_values('market_cap', ascending=False).reset_index(drop=True)

    def classify_size(rank):
        if rank < 100:
            return "大型株"
        elif rank < 500:
            return "中型株"
        else:
            return "小型株"

    ranked['規模'] = [classify_size(i) for i in range(len(ranked))]

    # 時価総額が取得できなかった銘柄は「小型株」扱いにしておく
    cap_df = cap_df.merge(ranked[['code', '規模']], on='code', how='left')
    cap_df['規模'] = cap_df['規模'].fillna("小型株")

    return cap_df[['code', '規模']]

def check_ytd_low(code):
    try:
        ticker = yf.Ticker(f"{code}.T")
        hist = ticker.history(period="ytd")
        if len(hist) < 2:
            return None
        
        ytd_low = hist['Low'].min()
        latest_low = hist['Low'].iloc[-1]
        
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

def make_tradingview_link_df(sub_df):
    """規模別一覧などで使う、TradingViewリンク付きの表示用データフレームを作る"""
    display_rows = []
    for _, row in sub_df.iterrows():
        tv_url = f"https://www.tradingview.com/symbols/TSE-{row['コード']}/#{row['銘柄名']}"
        display_rows.append({
            "銘柄名": tv_url,
            "コード": row['コード'],
            "市場": row['市場・商品区分'],
            "業種": row['33業種区分'],
        })
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

# --- サイドバー（検索条件） ---
st.sidebar.header("🔍 検索フィルター")

df_jpx = load_jpx_data()

if not df_jpx.empty:
    # 規模区分（大型/中型/小型）をYahoo!ファイナンスの時価総額から算出して付与
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
    codes_all = tuple(df_jpx['コード_str'].tolist())
    size_df = load_market_caps(codes_all)
    df_jpx = df_jpx.merge(size_df, left_on='コード_str', right_on='code', how='left')
    df_jpx['規模'] = df_jpx['規模'].fillna("小型株")

    st.sidebar.subheader("1. ターゲット設定")
    markets = ["すべて"] + list(df_jpx['市場・商品区分'].unique())
    selected_market = st.sidebar.selectbox("市場区分", markets)
    
    sectors = ["すべて"] + list(df_jpx['33業種区分'].unique())
    selected_sector = st.sidebar.selectbox("業種", sectors)

    sizes = ["すべて", "大型株", "中型株", "小型株"]
    selected_size = st.sidebar.selectbox("規模（時価総額ベース）", sizes)

    st.sidebar.markdown("---")
    st.sidebar.subheader("2. 価格条件")
    use_ytd_low = st.sidebar.checkbox("年初来安値を更新している銘柄のみ", value=True)
    
    st.sidebar.markdown("---")
    st.sidebar.subheader("3. 財務フィルター（チェック式）")
    
    col_s1, col_s2 = st.sidebar.columns([1, 2])
    use_per = col_s1.checkbox("PER", value=True)
    max_per = col_s2.number_input("PER上限(倍)", min_value=0.0, value=15.0, label_visibility="collapsed")
    
    col_s3, col_s4 = st.sidebar.columns([1, 2])
    use_roe = col_s3.checkbox("ROE", value=True)
    min_roe = col_s4.number_input("ROE下限(%)", min_value=0.0, value=8.0, label_visibility="collapsed")
    
    col_s5, col_s6 = st.sidebar.columns([1, 2])
    use_psr = col_s5.checkbox("PSR", value=False)
    max_psr = col_s6.number_input("PSR上限(倍)", min_value=0.0, value=5.0, label_visibility="collapsed")
    
    col_s7, col_s8 = st.sidebar.columns([1, 2])
    use_yield = col_s7.checkbox("利回り", value=False)
    min_yield = col_s8.number_input("利回り下限(%)", min_value=0.0, value=3.0, label_visibility="collapsed")
    
    st.sidebar.markdown("---")
    search_btn = st.sidebar.button("🚀 スクリーニング開始", type="primary", use_container_width=True)

# --- メイン画面（タブ構成） ---
st.title("📈 年初来安値 ＆ 財務スクリーニングダッシュボード")

tab_screen, tab_list = st.tabs(["🔍 スクリーニング", "📋 銘柄一覧（規模別）"])

# ============================================================
# タブ1: スクリーニング（検索ボタンを押して条件抽出）
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
        
        # ── サマリー表示エリア（メトリクス） ──
        m1, m2, m3 = st.columns(3)
        m1.metric(label="第1関門：対象銘柄数", value=f"{len(codes)} 件")

        if len(codes) == 0:
            st.warning("⚠️ 条件に合致する銘柄がありませんでした。市場・業種・規模の条件を見直してください。")
        else:
            # --- 年初来安値の条件（チェックが入っている場合のみ実行） ---
            if use_ytd_low:
                progress_text = "株価データをチェック中..."
                my_bar = st.progress(0, text=progress_text)
                ytd_low_codes = []
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(check_ytd_low, code): code for code in codes}
                    for i, future in enumerate(concurrent.futures.as_completed(futures)):
                        result = future.result()
                        if result:
                            ytd_low_codes.append(result)
                        my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")
                
                my_bar.empty()
                m2.metric(label="第2関門：年初来安値更新", value=f"{len(ytd_low_codes)} 件")
            else:
                ytd_low_codes = codes
                m2.metric(label="第2関門：年初来安値条件", value="条件なし（全銘柄通過）")
            
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
                                "銘柄名": tv_url,  # 表示上はURL（display_textで会社名に見せる）
                                "会社名": company_name,  # Discord通知用に別途保持
                                "市場": row['市場・商品区分'],
                                "業種": row['33業種区分'],
                                "規模": row['規模'],
                                "PER (倍)": round(data['PER'], 2) if data['PER'] else "-",
                                "ROE (%)": round(data['ROE'], 2) if data['ROE'] else "-",
                                "PSR (倍)": round(data['PSR'], 2) if data['PSR'] else "-",
                                "配当利回り (%)": round(data['Yield'], 2) if data['Yield'] else "-",
                            })
                
                m3.metric(label="最終条件クリア銘柄", value=f"{len(final_results)} 件")
                st.markdown("---")
                
                if final_results:
                    st.success(f"🎉 条件をクリアしたお宝候補が **{len(final_results)}件** 見つかりました！")
                    result_df = pd.DataFrame(final_results)
                    
                    st.dataframe(
                        result_df,
                        column_config={
                            "銘柄名": st.column_config.LinkColumn(
                                "銘柄名 (クリックでTradingViewへ)",
                                help="クリックしてチャート・財務を確認",
                                display_text=r".*#(.+)"
                            ),
                            "会社名": None  # Discord通知用の内部列なので画面には表示しない
                        },
                        use_container_width=True,
                        hide_index=True
                    )
                    
                    for res in final_results:
                        msg = f"【安値更新＆条件クリア】\n{res['会社名']} ({res['コード']})\n規模: {res['規模']}\nPER: {res['PER (倍)']} / ROE: {res['ROE (%)']}% / PSR: {res['PSR (倍)']} / 配当利回り: {res['配当利回り (%)']}%"
                        send_discord_notify(msg)
                else:
                    st.warning("⚠️ 指定した条件をすべてクリアした銘柄はありませんでした（条件を少し緩めると見つかる場合があります）。")
            else:
                m3.metric(label="最終条件クリア銘柄", value="0 件")
                st.warning("⚠️ 選択した市場・業種・規模の中で、条件に合致する銘柄はありませんでした。")
    elif not search_btn:
        st.info("👈 サイドバーで条件を設定して「スクリーニング開始」ボタンを押してください。")

# ============================================================
# タブ2: 銘柄一覧（規模別） - 検索ボタンなしでいつでも閲覧可能
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
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '大型株']))

        with size_tab_mid:
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '中型株']))

        with size_tab_small:
            render_link_table(make_tradingview_link_df(df_jpx[df_jpx['規模'] == '小型株']))
    else:
        st.info("銘柄データが読み込まれていません。data_j.xls を確認してください。")