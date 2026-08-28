import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures
import time

# --- セッションステートの初期化 ---
if "layout_mode" not in st.session_state:
    st.session_state.layout_mode = "desktop"
if "market_filter" not in st.session_state:
    st.session_state.market_filter = "すべて"
if "sector_filter" not in st.session_state:
    st.session_state.sector_filter = "すべて"
if "use_ytd" not in st.session_state:
    st.session_state.use_ytd = True
if "use_range" not in st.session_state:
    st.session_state.use_range = True
if "min_range" not in st.session_state:
    st.session_state.min_range = 15.0

# --- ページ設定 ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide" if st.session_state.layout_mode == "desktop" else "centered",
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

def get_fundamentals(code, max_retries=1):
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            ticker = yf.Ticker(f"{code}.T")
            info = ticker.info
            
            current_price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')
            if not current_price:
                hist = ticker.history(period="5d")
                if not hist.empty:
                    current_price = hist['Close'].iloc[-1]

            per = info.get('trailingPE') or info.get('forwardPE')
            if not per and current_price:
                eps = info.get('trailingEps') or info.get('forwardEps')
                if eps and eps > 0:
                    per = current_price / eps

            psr = info.get('priceToSalesTrailing12Months')
            if not psr and current_price:
                market_cap = info.get('marketCap')
                revenue = info.get('totalRevenue')
                if market_cap and revenue and revenue > 0:
                    psr = market_cap / revenue

            roe = info.get('returnOnEquity')
            if roe is not None:
                roe = roe * 100

            summary = info.get('longBusinessSummary', '事業概要の記載なし')

            return {
                'code': code, 
                'PER': per, 
                'ROE': roe, 
                'PSR': psr, 
                'summary': summary, 
                'error': None
            }
        except Exception as e:
            last_error = str(e)

        if attempt < max_retries:
            time.sleep(0.3)

    return {
        'code': code, 'PER': None, 'ROE': None, 
        'PSR': None, 'summary': '', 'error': last_error
    }

def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})

# --- データ読み込み ---
df_jpx = load_jpx_data()
if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())

# --- サイドバー：表示モード切り替え ---
st.sidebar.header("⚙️ 設定")
mode_label = st.sidebar.radio(
    "表示モード",
    ["🖥 デスクトップ", "📱 スマホ"],
    index=0 if st.session_state.layout_mode == "desktop" else 1,
    horizontal=True,
)
is_mobile = False if mode_label == "🖥 デスクトップ" else True
if ("desktop" if mode_label == "🖥 デスクトップ" else "mobile") != st.session_state.layout_mode:
    st.session_state.layout_mode = "desktop" if mode_label == "🖥 デスクトップ" else "mobile"
    st.rerun()

# --- メイン画面：フィルターバー ---
st.title("📈 株式スクリーニングダッシュボード")
st.markdown("条件を設定してスクリーニングを実行するか、全銘柄一覧タブをご確認ください。")

with st.container(border=True):
    st.markdown("##### 🎛️ フィルターバー")
    
    # 1. 業種・市場区分フィルター
    f1, f2 = st.columns(2)
    with f1:
        st.session_state.market_filter = st.selectbox("市場区分", market_options, index=market_options.index(st.session_state.market_filter) if st.session_state.market_filter in market_options else 0)
    with f2:
        st.session_state.sector_filter = st.selectbox("業種", sector_options, index=sector_options.index(st.session_state.sector_filter) if st.session_state.sector_filter in sector_options else 0)

    st.markdown("---")
    
    # 2. 年初来安値・値動き率（レンジ）フィルター
    p1, p2, p3 = st.columns([1.2, 1, 1.8])
    with p1:
        st.session_state.use_ytd = st.checkbox("年初来安値更新", value=st.session_state.use_ytd)
    with p2:
        st.session_state.use_range = st.checkbox("値動き率（レンジ）", value=st.session_state.use_range)
    with p3:
        st.session_state.min_range = st.number_input("最低レンジ幅 (%)", min_value=0.0, value=st.session_state.min_range, step=1.0, help="年初来の高値・安値の変動幅がこの割合以上の銘柄に絞ります")

    st.markdown("---")

    # 3. 財務条件フィルター（PER、PSR、ROE）
    st.markdown("###### 💰 財務条件フィルター（制限をかけたい項目にチェック）")
    t1, t2, t3 = st.columns(3)
    with t1:
        use_per = st.checkbox("PER制限", value=True)
        max_per = st.number_input("PER上限 (倍)", min_value=0.0, value=15.0, step=1.0)
    with t2:
        use_psr = st.checkbox("PSR制限", value=False)
        max_psr = st.number_input("PSR上限 (倍)", min_value=0.0, value=5.0, step=1.0)
    with t3:
        use_roe = st.checkbox("ROE制限", value=True)
        min_roe = st.number_input("ROE下限 (%)", min_value=0.0, value=8.0, step=1.0)

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
            if st.session_state.use_ytd:
                progress_text = "年初来安値＆値動き率を解析中..."
                my_bar = st.progress(0, text=progress_text)
                ytd_low_codes = []

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
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

            m1, m2 = st.columns(2)
            m1.metric("① 対象銘柄数", f"{len(codes)} 件")
            m2.metric("② 条件クリア", f"{len(ytd_low_codes)} 件")

            if len(ytd_low_codes) > 0:
                final_results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(get_fundamentals, code): code for code in ytd_low_codes}
                    for future in concurrent.futures.as_completed(futures):
                        data = future.result()
                        
                        per_ok = True
                        psr_ok = True
                        roe_ok = True
                        
                        if use_per:
                            per_ok = (data['PER'] is not None) and (data['PER'] <= max_per)
                        if use_psr:
                            psr_ok = (data['PSR'] is not None) and (data['PSR'] <= max_psr)
                        if use_roe:
                            roe_ok = (data['ROE'] is not None) and (data['ROE'] >= min_roe)
                            
                        if per_ok and psr_ok and roe_ok:
                            row = target_df[target_df['コード'].astype(str) == data['code']].iloc[0]
                            company_name = row['銘柄名']
                            code = data['code']
                            
                            tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{company_name}"
                            
                            final_results.append({
                                "コード": code,
                                "銘柄名": tv_url,
                                "会社名": company_name,
                                "市場": row['市場・商品区分'],
                                "業種": row['33業種区分'],
                                "PER (倍)": round(data['PER'], 2) if data['PER'] else "-",
                                "PSR (倍)": round(data['PSR'], 2) if data['PSR'] else "-",
                                "ROE (%)": round(data['ROE'], 2) if data['ROE'] else "-",
                                "summary": data['summary']
                            })
                
                st.markdown("---")
                if final_results:
                    st.success(f"🎉 条件をクリアした銘柄が **{len(final_results)}件** 見つかりました！")
                    result_df = pd.DataFrame(final_results)

                    column_config = {
                        "銘柄名": st.column_config.LinkColumn(
                            "銘柄名 (クリックでTradingViewへ)",
                            help="クリックしてチャートを確認",
                            display_text=r".*#(.+)"
                        ),
                        "会社名": None,
                        "summary": None
                    }
                    
                    st.dataframe(
                        result_df,
                        column_config=column_config,
                        use_container_width=True,
                        hide_index=True
                    )
                    
                    # ブラウザAI用プロンプト生成
                    st.markdown("### 📋 ブラウザAI用プロンプト生成")
                    for res in final_results:
                        with st.container(border=True):
                            st.markdown(f"#### 🏢 {res['会社Name'] if '会社Name' in res else res['会社名']} <span style='font-size:0.8em; color:gray;'>({res['コード']})</span>", unsafe_allow_html=True)
                            st.caption(f"📊 PER: **{res['PER (倍)']}倍** | PSR: **{res['PSR (倍)']}倍** | ROE: **{res['ROE (%)']}%**")
                            
                            prompt_text = f"""以下の日本株企業についてファンダメンタル分析をまとめてください。
【企業名】 {res['会社名']} ({res['コード']})
【事業概要】 {res['summary']}
【主要指標】 PER: {res['PER (倍)']}倍 / PSR: {res['PSR (倍)']}倍 / ROE: {res['ROE (%)']}%
強み、割安感、注目ポイントを3行程度で要約してください。"""

                            with st.expander("📝 AI用プロンプト（コピー用）"):
                                st.code(prompt_text, language="markdown")

                    for res in final_results:
                        msg = f"【スクリーニングヒット】\n{res['会社名']} ({res['コード']})\nPER: {res['PER (倍)']} / ROE: {res['ROE (%)']}%"
                        send_discord_notify(msg)
                else:
                    st.warning("⚠️ 指定した財務条件をクリアした銘柄はありませんでした。")
            else:
                st.warning("⚠️ 年初来安値・値動き率の条件に合致する銘柄はありませんでした。")
    elif not search_btn:
        st.info("👆 上部のフィルターバーで条件を設定して「スクリーニングを実行する」ボタンを押してください。")

# ============================================================
# タブ2: 全銘柄一覧
# ============================================================
with tab_list:
    st.markdown("全銘柄の一覧です。気になるコードを入力するか、リストから確認してください。")
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
                    st.markdown(f"**[{c_name}]({tv_url})** （コード: `{code}`）")
                    with st.spinner("財務指標を取得中..."):
                        f_data = get_fundamentals(code)
                        st.caption(f"PER: {round(f_data['PER'], 2) if f_data['PER'] else '-'}倍 / PSR: {round(f_data['PSR'], 2) if f_data['PSR'] else '-'}倍 / ROE: {round(f_data['ROE'], 2) if f_data['ROE'] else '-'}%")
                        
                        p_text = f"""以下の日本株企業についてファンダメンタル分析をまとめてください。
【企業名】 {c_name} ({code})
【事業概要】 {f_data['summary']}
【主要指標】 PER: {round(f_data['PER'], 2) if f_data['PER'] else '-'}倍 / PSR: {round(f_data['PSR'], 2) if f_data['PSR'] else '-'}倍 / ROE: {round(f_data['ROE'], 2) if f_data['ROE'] else '-'}%
強み、割安感、注目ポイントを3行程度で要約してください。"""
                        st.code(p_text, language="markdown")
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
