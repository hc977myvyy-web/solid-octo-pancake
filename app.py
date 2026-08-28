import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures
from openai import OpenAI

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

# --- ページ設定 ---
st.set_page_config(
    page_title="株式スクリーニングツール",
    page_icon="📈",
    layout="wide" if st.session_state.layout_mode == "desktop" else "centered",
)

# --- 設定 (Discord & OpenAI API) ---
try:
    DISCORD_WEBHOOK_URL = st.secrets["DISCORD_WEBHOOK_URL"]
except Exception:
    DISCORD_WEBHOOK_URL = ""

try:
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    openai_client = None

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
        summary = info.get('longBusinessSummary', '事業概要の記載なし')
        
        if roe is not None:
            roe = roe * 100 
        if dividend_yield is not None:
            dividend_yield = dividend_yield * 100
            
        return {'code': code, 'PER': per, 'ROE': roe, 'PSR': psr, 'Yield': dividend_yield, 'summary': summary}
    except:
        return {'code': code, 'PER': None, 'ROE': None, 'PSR': None, 'Yield': None, 'summary': ''}

def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})

def get_ai_summary(company_name, code, metrics, summary):
    if not openai_client:
        return "⚠️ OpenAI APIキーが設定されていません（Streamlit Secretsの 'OPENAI_API_KEY' を確認してください）。"
    
    prompt = f"""
    以下の企業について、個人投資家向けにファンダメンタル分析を簡潔にまとめてください。
    
    【企業名】 {company_name} ({code})
    【事業概要】 {summary}
    【主要指標】
    - PER: {metrics.get('PER', '-')}倍
    - PSR: {metrics.get('PSR', '-')}倍
    - ROE: {metrics.get('ROE', '-')}%
    - 配当利回り: {metrics.get('Yield', '-')}%
    
    以下の構成で3〜4行程度で極めて簡潔に要約してください：
    1. **ビジネスの強み**: 
    2. **現在の評価・割安感**: 
    3. **注目ポイント**: 
    """
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "あなたは優秀な日本株のアナリストです。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"ChatGPTによる要約の生成に失敗しました: {e}"

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
st.markdown("フィルターバーから条件を設定し、スクリーニングを実行してください。")

with st.container(border=True):
    st.markdown("##### 🎛️ フィルターバー")
    
    f1, f2, f3 = st.columns(3)
    with f1:
        st.session_state.market_filter = st.selectbox("市場区分", market_options, index=market_options.index(st.session_state.market_filter) if st.session_state.market_filter in market_options else 0)
    with f2:
        st.session_state.sector_filter = st.selectbox("業種", sector_options, index=sector_options.index(st.session_state.sector_filter) if st.session_state.sector_filter in sector_options else 0)
    with f3:
        st.session_state.size_filter = st.selectbox("規模（時価総額）", size_options, index=size_options.index(st.session_state.size_filter) if st.session_state.size_filter in size_options else 0)

    st.markdown("---")
    
    p1, p2, p3 = st.columns([1.2, 1, 1.8])
    with p1:
        st.session_state.use_ytd = st.checkbox("年初来安値更新", value=st.session_state.use_ytd)
    with p2:
        st.session_state.use_range = st.checkbox("レンジ下限絞り", value=st.session_state.use_range)
    with p3:
        st.session_state.min_range = st.number_input("レンジ下限(%)", min_value=0.0, value=st.session_state.min_range, step=1.0)

    st.markdown("---")

    st.markdown("###### 💰 財務条件フィルター（制限をかけたい項目にチェックを入れてください）")
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        use_per = st.checkbox("PER制限", value=True)
        max_per = st.number_input("PER上限(倍)", min_value=0.0, value=15.0, step=1.0)
    with t2:
        use_psr = st.checkbox("PSR制限", value=False)
        max_psr = st.number_input("PSR上限(倍)", min_value=0.0, value=5.0, step=1.0)
    with t3:
        use_roe = st.checkbox("ROE制限", value=True)
        min_roe = st.number_input("ROE下限(%)", min_value=0.0, value=8.0, step=1.0)
    with t4:
        use_yield = st.checkbox("配当利回り制限", value=False)
        min_yield = st.number_input("利回り下限(%)", min_value=0.0, value=3.0, step=1.0)

    st.markdown("")
    search_btn = st.button("🚀 スクリーニングを実行する", type="primary", use_container_width=True)

st.markdown("---")
tab_screen, tab_list = st.tabs(["🔍 スクリーニング結果", "📋 全銘柄一覧 ＆ AI分析"])

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
                        psr_ok = True
                        roe_ok = True
                        yield_ok = True
                        
                        if use_per:
                            per_ok = (data['PER'] is not None) and (data['PER'] <= max_per)
                        if use_psr:
                            psr_ok = (data['PSR'] is not None) and (data['PSR'] <= max_psr)
                        if use_roe:
                            roe_ok = (data['ROE'] is not None) and (data['ROE'] >= min_roe)
                        if use_yield:
                            yield_ok = (data['Yield'] is not None) and (data['Yield'] >= min_yield)
                            
                        if per_ok and psr_ok and roe_ok and yield_ok:
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
                                "規模": row['規模'],
                                "PER (倍)": round(data['PER'], 2) if data['PER'] else "-",
                                "PSR (倍)": round(data['PSR'], 2) if data['PSR'] else "-",
                                "ROE (%)": round(data['ROE'], 2) if data['ROE'] else "-",
                                "配当利回り (%)": round(data['Yield'], 2) if data['Yield'] else "-",
                                "summary": data['summary']
                            })
                
                st.markdown("---")
                if final_results:
                    st.success(f"🎉 条件をクリアしたお宝候補が **{len(final_results)}件** 見つかりました！")
                    result_df = pd.DataFrame(final_results)

                    column_config = {
                        "銘柄名": st.column_config.LinkColumn(
                            "銘柄名 (クリックでTradingView/アプリへ)",
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
                    
                    st.markdown("### 🤖 抽出銘柄のChatGPTファンダメンタル分析")
                    for res in final_results:
                        with st.container(border=True):
                            c_col1, c_col2 = st.columns([3, 1])
                            c_col1.markdown(f"#### 🏢 {res['会社名']} <span style='font-size:0.8em; color:gray;'>({res['コード']})</span>", unsafe_allow_html=True)
                            c_col2.markdown(f"**規模:** {res['規模']}")
                            
                            st.caption(f"📊 指標 | PER: **{res['PER (倍)']}倍** | PSR: **{res['PSR (倍)']}倍** | ROE: **{res['ROE (%)']}%** | 配当利回り: **{res['配当利回り (%)']}%**")
                            
                            with st.expander("✨ ChatGPTによるファンダメンタル分析結果を見る"):
                                with st.spinner(f"{res['会社名']} の分析を生成中..."):
                                    summary_text = get_ai_summary(
                                        res['会社名'], 
                                        res['コード'], 
                                        {
                                            'PER': res['PER (倍)'], 
                                            'PSR': res['PSR (倍)'], 
                                            'ROE': res['ROE (%)'], 
                                            'Yield': res['配当利回り (%)']
                                        }, 
                                        res['summary']
                                    )
                                    st.markdown(summary_text)

                    for res in final_results:
                        msg = f"【安値更新＆条件クリア】\n{res['会社名']} ({res['コード']})\n規模: {res['規模']}\nPER: {res['PER (倍)']} / PSR: {res['PSR (倍)']} / ROE: {res['ROE (%)']}% / 配当利回り: {res['配当利回り (%)']}%"
                        send_discord_notify(msg)
                else:
                    st.warning("⚠️ 指定した財務制限をすべてクリアした銘柄はありませんでした。")
            else:
                st.warning("⚠️ 価格条件に合致する銘柄はありませんでした。")
    elif not search_btn:
        st.info("👆 上部のフィルターバーで条件を設定してコードのスクリーニングを実行してください。")

# ============================================================
# タブ2: 全銘柄一覧 ＆ AI分析（規模別タブ）
# ============================================================
with tab_list:
    st.markdown("規模区分ごとの全銘柄一覧です。気になる企業のコードを入力して個別にChatGPT分析を行うこともできます。")
    st.markdown("---")

    if not df_jpx.empty:
        with st.container(border=True):
            st.markdown("##### 🔍 任意銘柄のピンポイントChatGPT分析")
            search_code_input = st.text_input("銘柄コードを入力してください（例: 4792, 7203）", value="")
            if search_code_input:
                target_row = df_jpx[df_jpx['コード'].astype(str) == search_code_input.strip()]
                if not target_row.empty:
                    c_name = target_row.iloc[0]['銘柄名']
                    c_market = target_row.iloc[0]['市場・商品区分']
                    c_sector = target_row.iloc[0]['33業種区分']
                    c_size = target_row.iloc[0]['規模']
                    
                    st.success(f"**対象企業: {c_name} ({search_code_input})** / 市場: {c_market} / 業種: {c_sector} / 規模: {c_size}")
                    
                    with st.spinner("リアルタイムデータを取得してChatGPTが分析中..."):
                        fund_data = get_fundamentals(search_code_input.strip())
                        ai_res_text = get_ai_summary(
                            c_name, 
                            search_code_input.strip(), 
                            {
                                'PER': round(fund_data['PER'], 2) if fund_data['PER'] else '-', 
                                'PSR': round(fund_data['PSR'], 2) if fund_data['PSR'] else '-', 
                                'ROE': round(fund_data['ROE'], 2) if fund_data['ROE'] else '-', 
                                'Yield': round(fund_data['Yield'], 2) if fund_data['Yield'] else '-'
                            }, 
                            fund_data['summary']
                        )
                        st.markdown(ai_res_text)
                else:
                    st.error("該当する銘柄コードが見つかりませんでした。")

        st.markdown("---")
        
        size_tab_large, size_tab_mid, size_tab_small = st.tabs([
            f"🔵 大型株（{len(df_jpx[df_jpx['規模'] == '大型株'])}件）",
            f"🟢 中型株（{len(df_jpx[df_jpx['規模'] == '中型株'])}件）",
            f"⚪ 小型株（{len(df_jpx[df_jpx['規模'] == '小型株'])}件）",
        ])

        def render_all_list_with_ai(sub_df):
            for _, row in sub_df.iterrows():
                code = row['コード']
                name = row['銘柄名']
                tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{name}"
                
                with st.container(border=True):
                    col_a, col_b = st.columns([3, 1])
                    col_a.markdown(f"**[{name}]({tv_url})** （コード: `{code}`） / 業種: {row['33業種区分']}")
                    
                    with st.expander("🤖 この企業のChatGPT分析を見る"):
                        with st.spinner("ChatGPTが分析を生成中..."):
                            f_data = get_fundamentals(str(code))
                            res_text = get_ai_summary(
                                name, 
                                str(code), 
                                {
                                    'PER': round(f_data['PER'], 2) if f_data['PER'] else '-', 
                                    'PSR': round(f_data['PSR'], 2) if f_data['PSR'] else '-', 
                                    'ROE': round(f_data['ROE'], 2) if f_data['ROE'] else '-', 
                                    'Yield': round(f_data['Yield'], 2) if f_data['Yield'] else '-'
                                }, 
                                f_data['summary']
                            )
                            st.markdown(res_text)

        with size_tab_large:
            st.caption("大型株の一覧と各企業のChatGPT分析")
            render_all_list_with_ai(df_jpx[df_jpx['規模'] == '大型株'])
        with size_tab_mid:
            st.caption("中型株の一覧と各企業のChatGPT分析")
            render_all_list_with_ai(df_jpx[df_jpx['規模'] == '中型株'])
        with size_tab_small:
            st.caption("小型株の一覧と各企業のChatGPT分析")
            render_all_list_with_ai(df_jpx[df_jpx['規模'] == '小型株'])
    else:
        st.info("銘柄データが読み込まれていません。")
