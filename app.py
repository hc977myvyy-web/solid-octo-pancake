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
        st.error(f"銘柄データの取得に失敗しました: {e}")
        return pd.DataFrame()


def get_market_cap(code, max_retries=2):
    """
    時価総額を取得する。
    - まず軽量な fast_info を試す(レート制限に引っかかりにくい)
    - 失敗したら info にフォールバック
    - それでも失敗したら少し待って最大 max_retries 回リトライ
    - 最終的に失敗した場合はエラー内容も一緒に返す(握りつぶさない)
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            ticker = yf.Ticker(f"{code}.T")

            # 1. fast_info を優先(軽量・高速・レート制限を受けにくい)
            try:
                cap = ticker.fast_info.get("market_cap")
                if cap:
                    return {"code": code, "market_cap": cap, "error": None}
            except Exception:
                pass  # fast_info がダメなら info にフォールバック

            # 2. info にフォールバック
            info = ticker.info
            cap = info.get('marketCap')
            if cap:
                return {"code": code, "market_cap": cap, "error": None}

            last_error = "marketCap が取得できませんでした"
        except Exception as e:
            last_error = str(e)

        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))  # 少しずつ待ってリトライ(レート制限対策)

    return {"code": code, "market_cap": None, "error": last_error}


@st.cache_data(ttl=86400)
def load_market_caps(codes):
    results = []
    progress_text = "時価総額データを取得中（全銘柄の規模分類を行っています）..."
    my_bar = st.progress(0, text=progress_text)

    # 並列数を下げてAPI負荷・レート制限を軽減(10→5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_market_cap, code): code for code in codes}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            results.append(future.result())
            my_bar.progress((i + 1) / len(codes), text=f"{progress_text} ({i+1}/{len(codes)})")

    my_bar.empty()

    cap_df = pd.DataFrame(results)

    success_count = cap_df['market_cap'].notna().sum()
    fail_count = len(cap_df) - success_count
    if fail_count > 0:
        st.warning(
            f"⚠️ 時価総額の取得に失敗した銘柄が {fail_count} 件あります"
            f"（成功 {success_count} / {len(cap_df)} 件）。"
            "失敗した銘柄は「分類不可」として扱われ、規模フィルターでは除外されます。"
        )

    # 時価総額が大きい順にソートしてランキング付け(取得できたものだけ)
    ranked = cap_df.dropna(subset=['market_cap']).sort_values('market_cap', ascending=False).reset_index(drop=True)

    def classify_size(rank):
        if rank < 100:
            return "大型株"
        elif rank < 500:
            return "中型株"
        else:
            return "小型株"

    ranked['規模'] = [classify_size(i) for i in range(len(ranked))]

    # 元のデータフレームに規模をマージ
    cap_df = cap_df.merge(ranked[['code', '規模']], on='code', how='left')

    # ★修正ポイント★
    # これまでは fillna("小型株") で「取得失敗 = 小型株」に強制していたが、
    # これだと大型株がAPIエラーで小型株に化けてしまいバグの原因になっていた。
    # 取得失敗は「分類不可」として明示し、小型株と混ぜない。
    cap_df['規模'] = cap_df['規模'].fillna("分類不可")

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

def get_fundamentals(code, max_retries=2):
    """
    財務指標(PER/ROE/PSR/配当利回り)を取得する。
    - 一時的な失敗はリトライで拾う
    - 何が原因で取得できなかったかを error として保持する(握りつぶさない)
    - 1項目でも取れていれば結果として採用する(全部空になるのを防ぐ)
    """
    last_error = None
    for attempt in range(max_retries + 1):
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

            if any(v is not None for v in [per, roe, psr, dividend_yield]):
                return {'code': code, 'PER': per, 'ROE': roe, 'PSR': psr,
                        'Yield': dividend_yield, 'summary': summary, 'error': None}
            last_error = "指標がすべて空でした"
        except Exception as e:
            last_error = str(e)

        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))  # 少し待ってリトライ(レート制限対策)

    return {'code': code, 'PER': None, 'ROE': None, 'PSR': None,
            'Yield': None, 'summary': '', 'error': last_error}

def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})

# --- データ読み込み ＆ 規模分類の適用 ---
df_jpx = load_jpx_data()
if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].astype(str)
    codes_all = tuple(df_jpx['コード_str'].tolist())
    size_df = load_market_caps(codes_all)
    
    # 既存の規模列があれば削除してマージし直す
    if '規模' in df_jpx.columns:
        df_jpx = df_jpx.drop(columns=['規模'])
    df_jpx = df_jpx.merge(size_df, left_on='コード_str', right_on='code', how='left')
    df_jpx['規模'] = df_jpx['規模'].fillna("分類不可")

    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())
    size_options = ["すべて", "大型株", "中型株", "小型株", "分類不可"]

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

# --- サイドバー：キャッシュクリア(時価総額データがおかしいと感じたら使用) ---
if st.sidebar.button("🔄 時価総額データを再取得する"):
    load_market_caps.clear()
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
tab_screen, tab_list = st.tabs(["🔍 スクリーニング結果", "📋 全銘柄一覧 ＆ ブラウザAI連携"])

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
                fundamentals_fail_count = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(get_fundamentals, code): code for code in ytd_low_codes}
                    for future in concurrent.futures.as_completed(futures):
                        data = future.result()
                        if data.get('error'):
                            fundamentals_fail_count += 1

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
                if fundamentals_fail_count > 0:
                    st.warning(
                        f"⚠️ 財務指標(PER/ROE/PSR/配当利回り)の取得に失敗した銘柄が "
                        f"{fundamentals_fail_count} 件ありました。"
                        "これらは財務条件フィルターで自動的に除外されています。"
                        "件数が多い場合は少し時間を置いて再実行してみてください。"
                    )
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
                    
                    # --- 各企業ごとのブラウザAI用コピープロンプトセクション ---
                    st.markdown("### 📋 ブラウザAI（ChatGPT/Gemini）用プロンプト生成")
                    st.caption("ボタン（コードブロックの右上）をクリックしてコピーし、無料版のChatGPTやGeminiに貼り付けて分析させてください。")
                    
                    for res in final_results:
                        with st.container(border=True):
                            c_col1, c_col2 = st.columns([3, 1])
                            c_col1.markdown(f"#### 🏢 {res['会社名']} <span style='font-size:0.8em; color:gray;'>({res['コード']})</span>", unsafe_allow_html=True)
                            c_col2.markdown(f"**規模:** {res['規模']}")
                            
                            st.caption(f"📊 指標 | PER: **{res['PER (倍)']}倍** | PSR: **{res['PSR (倍)']}倍** | ROE: **{res['ROE (%)']}%** | 配当利回り: **{res['配当利回り (%)']}%**")
                            
                            # ブラウザAIにそのまま貼り付けられる綺麗なプロンプト文章を生成
                            prompt_text = f"""以下の日本株企業について、個人投資家向けにファンダメンタル分析を簡潔にまとめてください。

【企業名】 {res['会社名']} ({res['コード']})
【事業概要】 {res['summary']}
【主要指標】
- PER: {res['PER (倍)']}倍
- PSR: {res['PSR (倍)']}倍
- ROE: {res['ROE (%)']}%
- 配当利回り: {res['配当利回り (%)']}%

以下の構成で3〜4行程度で簡潔に要約してください：
1. **ビジネスの強み**: 
2. **現在の評価・割安感**: 
3. **注目ポイント**: """

                            with st.expander("📝 AI用プロンプト（コピー用）を表示"):
                                st.code(prompt_text, language="markdown")

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
# タブ2: 全銘柄一覧 ＆ ブラウザAI連携（規模別タブ）
# ============================================================
with tab_list:
    st.markdown("規模区分ごとの全銘柄一覧です。時価総額に基づき正確に「大型株・中型株・小型株」に分類されています(取得できなかった銘柄は「分類不可」)。")
    st.markdown("---")

    if not df_jpx.empty:
        size_tab_large, size_tab_mid, size_tab_small, size_tab_unknown = st.tabs([
            f"🔵 大型株（上位100社 / {len(df_jpx[df_jpx['規模'] == '大型株'])}件）",
            f"🟢 中型株（100〜500位 / {len(df_jpx[df_jpx['規模'] == '中型株'])}件）",
            f"⚪ 小型株（500位以降 / {len(df_jpx[df_jpx['規模'] == '小型株'])}件）",
            f"❓ 分類不可（データ取得失敗 / {len(df_jpx[df_jpx['規模'] == '分類不可'])}件）",
        ])

        def render_all_list_with_copy(sub_df):
            for _, row in sub_df.iterrows():
                code = row['コード']
                name = row['銘柄名']
                tv_url = f"https://www.tradingview.com/symbols/TSE-{code}/#{name}"
                
                with st.container(border=True):
                    col_a, col_b = st.columns([3, 1])
                    col_a.markdown(f"**[{name}]({tv_url})** （コード: `{code}`） / 市場: {row['市場・商品区分']} / 業種: {row['33業種区分']}")
                    
                    with st.expander("📝 ブラウザAI用プロンプトをコピーする"):
                        with st.spinner("企業データを取得中..."):
                            f_data = get_fundamentals(str(code))
                            p_text = f"""以下の日本株企業についてファンダメンタル分析をまとめてください。
【企業名】 {name} ({code})
【事業概要】 {f_data['summary']}
【主要指標】
- PER: {round(f_data['PER'], 2) if f_data['PER'] else '-'}倍
- PSR: {round(f_data['PSR'], 2) if f_data['PSR'] else '-'}倍
- ROE: {round(f_data['ROE'], 2) if f_data['ROE'] else '-'}%
- 配当利回り: {round(f_data['Yield'], 2) if f_data['Yield'] else '-'}%

強み、割安感、注目ポイントを3行程度で要約してください。"""
                            st.code(p_text, language="markdown")

        with size_tab_large:
            st.caption("時価総額上位約100社の大型株一覧")
            render_all_list_with_copy(df_jpx[df_jpx['規模'] == '大型株'])
        with size_tab_mid:
            st.caption("時価総額100位〜500位の中型株一覧")
            render_all_list_with_copy(df_jpx[df_jpx['規模'] == '中型株'])
        with size_tab_small:
            st.caption("時価総額500位以降の小型株一覧")
            render_all_list_with_copy(df_jpx[df_jpx['規模'] == '小型株'])
        with size_tab_unknown:
            st.caption("時価総額データの取得に失敗した銘柄一覧(再取得ボタンをお試しください)")
            render_all_list_with_copy(df_jpx[df_jpx['規模'] == '分類不可'])
    else:
        st.info("銘柄データが読み込まれていません。")
