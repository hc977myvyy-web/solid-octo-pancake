import time
import json
import threading
import urllib.parse
import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import concurrent.futures
from datetime import date, timedelta

# --- セッションステートの初期化 ---
_DEFAULTS = {
    "market_filter": "すべて",
    "sector_filter": "すべて",
    "data_source": "J-Quants",
    "use_ytd_low": True,
    "use_decline": True,
    "use_ore_teki": False,
    "exclude_today": True,
    "yf_fallback": True,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# --- スクリーニング条件（固定値） ---
LOOKBACK_DAYS = 20
MIN_AVG_VOLUME = 10000
DECLINE_THRESHOLD_PCT = 20.0
DECLINE_LOOKBACK_DAYS = 92
ORE_TEKI_PRICE_MIN = 1000.0
ORE_TEKI_PRICE_MAX = 2000.0

st.set_page_config(page_title="株価スクリーニング", page_icon="📈", layout="wide")


def _secret(name, default=""):
    try:
        return st.secrets[name]
    except Exception:
        return default


DISCORD_WEBHOOK_URL = _secret("DISCORD_WEBHOOK_URL")
JQUANTS_API_KEY_SECRET = _secret("JQUANTS_API_KEY")
ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")

JQUANTS_BASE_URL = "https://api.jquants.com/v2"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# プラン別上限: Free 5 / Light 60 / Standard 120 / Premium 500 (req/分)
JQ_RATE_LIMIT_PER_MIN = 55
JQ_MAX_WORKERS = 4


# ============================================================
# 共通ユーティリティ
# ============================================================

def normalize_code(value):
    """data_j.xls のコード列を文字列に正規化（'1301.0' や英文字入りコードに対応）。"""
    if pd.isna(value):
        return ""
    s = str(value).strip()
    return s[:-2] if s.endswith(".0") else s


def to_4digit(code5):
    """J-Quantsの5桁コード→4桁。末尾が0でないもの（優先株等）は None。"""
    s = str(code5)
    return s[:4] if len(s) == 5 and s.endswith("0") else None


def _num(v):
    """J-Quantsは数値も文字列で返すことがあり、欠損は空文字。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def format_mktcap(yen):
    """円 → '◯億円' / '◯兆円'。"""
    if yen is None or (isinstance(yen, float) and pd.isna(yen)):
        return "-"
    oku = yen / 1e8
    if abs(oku) >= 10000:
        return f"{oku / 10000:,.2f}兆円"
    if abs(oku) >= 1:
        return f"{oku:,.0f}億円"
    return f"{yen / 1e6:,.0f}百万円"


def format_ratio(v, unit="倍"):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v <= 0:
        return "-"
    return f"{v:,.2f}{unit}"


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


# J-Quantsへのリクエストは全てこの1つのリミッタを通す
JQ_LIMITER = RateLimiter(JQ_RATE_LIMIT_PER_MIN)


def classify_size(scale_label):
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
        df = df[df['市場・商品区分'].str.contains('内国株式', na=False)]
        if '規模区分' in df.columns:
            df['規模カテゴリ'] = df['規模区分'].apply(classify_size)
        return df
    except Exception as e:
        st.error(f"銘柄データの取得に失敗しました: data_j.xls ファイルを確認してください: {e}")
        return pd.DataFrame()


# ============================================================
# J-Quants 取得レイヤー
# ============================================================

def jq_request(path, params, api_key, errors, max_retries=4):
    """J-Quants API v2 への1リクエスト（pagination対応・429リトライ・エラーは errors に記録）。"""
    url = JQUANTS_BASE_URL + path
    headers = {"x-api-key": api_key}
    params = dict(params)
    records = []
    pagination_key = None
    label = params.get("date") or params.get("code") or ""

    while True:
        if pagination_key:
            params["pagination_key"] = pagination_key

        res = None
        for attempt in range(max_retries):
            JQ_LIMITER.acquire()
            try:
                res = requests.get(url, headers=headers, params=params, timeout=30)
            except Exception as e:
                errors.append(f"{path} {label}: 通信エラー {e}")
                return records
            if res.status_code == 200:
                break
            if res.status_code == 429:
                time.sleep(30 * (attempt + 1))
                res = None
                continue
            errors.append(f"{path} {label}: HTTP {res.status_code} {res.text[:200]}")
            return records

        if res is None:
            errors.append(f"{path} {label}: 429が続いたため中断")
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
    期間内の全上場銘柄の日足を日付ごとに一括取得する。
    date 指定なら1リクエストで全銘柄が取れるため、銘柄ループ（約4000req）を避けられる。
    戻り値: ({4桁コード: DataFrame(High/Low/Close/Volume/MktCap)}, errors)
    """
    if not api_key:
        return {}, ["APIキーが設定されていません。"]

    dates = pd.bdate_range(from_date_str, to_date_str)
    if len(dates) == 0:
        return {}, []

    errors, all_records = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=JQ_MAX_WORKERS) as ex:
        futures = [
            ex.submit(jq_request, "/equities/bars/daily",
                      {"date": d.strftime("%Y-%m-%d")}, api_key, errors)
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
        errors.append("レスポンスに Date / Code 列がありません。")
        return {}, errors

    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df["Code4"] = df["Code"].astype(str).map(to_4digit)
    df = df[df["Code4"].notna()]

    # 調整済み（分割・併合・ライツイシュー）を使用
    df = df.rename(columns={"AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"})
    if "MktCap" not in df.columns:
        df["MktCap"] = pd.NA

    need = ["Date", "Code4", "High", "Low", "Close", "Volume", "MktCap"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        errors.append(f"レスポンスに必要な列がありません: {missing}")
        return {}, errors

    df = df[need].sort_values("Date")
    panel = {
        c: g.set_index("Date")[["High", "Low", "Close", "Volume", "MktCap"]]
        for c, g in df.groupby("Code4", sort=False)
    }
    return panel, errors


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jquants_snapshot(api_key, asof_str):
    """
    直近営業日の全銘柄スナップショット（時価総額・終値）。
    全銘柄一覧タブ用。date指定なので1〜数リクエストで済む。
    """
    if not api_key:
        return {}
    errors = []
    d = pd.Timestamp(asof_str)
    for _ in range(8):
        while d.weekday() >= 5:
            d -= pd.Timedelta(days=1)
        recs = jq_request("/equities/bars/daily", {"date": d.strftime("%Y-%m-%d")}, api_key, errors)
        if recs:
            out = {}
            for r in recs:
                c4 = to_4digit(str(r.get("Code")))
                if not c4:
                    continue
                mc = _num(r.get("MktCap"))
                out[c4] = {
                    # MktCapは百万円単位なので円に直す
                    "mktcap": mc * 1e6 if mc is not None else None,
                    "close": _num(r.get("AdjC")) if _num(r.get("AdjC")) is not None else _num(r.get("C")),
                }
            return out
        d -= pd.Timedelta(days=1)
    return {}


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_jquants_fins(code, api_key):
    """
    /fins/summary から最新の決算サマリーを取り、PBR/PSR/PERの計算材料を抽出する。
    Lightプランでも利用可能。code指定なら1リクエストで全期間ぶん返る。
    """
    if not api_key:
        return {}
    errors = []
    recs = jq_request("/fins/summary", {"code": code}, api_key, errors)
    if not recs:
        return {}

    recs = sorted(recs, key=lambda r: (str(r.get("DiscDate") or ""), str(r.get("DiscNo") or "")))
    latest = recs[-1]

    out = {
        "disc_date": latest.get("DiscDate"),
        "doc_type": latest.get("DocType"),
        "period": latest.get("CurPerType"),
        "bps": _num(latest.get("BPS")),
        "eps_forecast": _num(latest.get("FEPS")),
        "eps_actual": _num(latest.get("EPS")),
        "equity": _num(latest.get("ShEq")) if _num(latest.get("ShEq")) else _num(latest.get("Eq")),
        "roe": _num(latest.get("ROE")),
        "op": _num(latest.get("OP")),
        "op_forecast": _num(latest.get("FOP")),
        "equity_ratio": _num(latest.get("EqAR")),
    }

    # PSR用の年間売上高：会社予想(通期) → 直近の通期実績 の順で採用
    annual, src = _num(latest.get("FSales")), "会社予想(通期)"
    if annual is None:
        for r in reversed(recs):
            if str(r.get("CurPerType")) == "FY" and _num(r.get("Sales")):
                annual, src = _num(r.get("Sales")), "直近通期実績"
                break
    out["annual_sales"], out["sales_src"] = annual, (src if annual else None)
    return out


def fetch_jquants_single(code, from_date_str, to_date_str, api_key):
    """単一銘柄の日足（個別検索用）。"""
    errors = []
    recs = jq_request("/equities/bars/daily",
                      {"code": code, "from": from_date_str, "to": to_date_str}, api_key, errors)
    if errors:
        st.warning(" / ".join(errors[:3]))
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    if "Date" not in df.columns:
        return pd.DataFrame()
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df = df.sort_values("Date").set_index("Date")
    df = df.rename(columns={"AdjH": "High", "AdjL": "Low", "AdjC": "Close", "AdjVo": "Volume"})
    keep = [c for c in ["High", "Low", "Close", "Volume", "MktCap"] if c in df.columns]
    return df[keep]


# ============================================================
# yfinance 取得レイヤー
# ============================================================

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_yfinance_history(code, from_date_str, to_date_str):
    """
    auto_adjust=False が重要。Yahooの High/Low/Close は分割調整済み・配当未調整なので、
    J-Quantsの AdjH/AdjL/AdjC（分割・併合・ライツイシューのみ調整）と意味が揃う。
    """
    try:
        ticker = yf.Ticker(f"{code}.T")
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
def fetch_yf_info(code):
    try:
        return yf.Ticker(f"{code}.T").info or {}
    except Exception:
        return {}


# ============================================================
# バリュエーション（J-Quants優先、足りない部分をyfinanceで補完）
# ============================================================

def get_valuation(code, source, api_key, mktcap_yen=None, close=None, allow_yf=True):
    """
    時価総額 / PER / PBR / PSR を返す。
      - J-Quants: 時価総額は日足の MktCap、PBRはBPS、PSRは年間売上高、PERは会社予想EPS
      - 不足分のみ yfinance の info で補完する
    """
    v = {"mktcap": mktcap_yen, "per": None, "pbr": None, "psr": None,
         "src": [], "sales_src": None, "disc_date": None, "roe": None}

    if source == "J-Quants" and api_key:
        f = fetch_jquants_fins(code, api_key)
        if f:
            v["disc_date"], v["roe"], v["sales_src"] = f.get("disc_date"), f.get("roe"), f.get("sales_src")
            eps = f.get("eps_forecast") or f.get("eps_actual")
            if close and eps and eps > 0:
                v["per"] = close / eps
            if close and f.get("bps") and f["bps"] > 0:
                v["pbr"] = close / f["bps"]
            elif v["mktcap"] and f.get("equity") and f["equity"] > 0:
                v["pbr"] = v["mktcap"] / f["equity"]
            if v["mktcap"] and f.get("annual_sales") and f["annual_sales"] > 0:
                v["psr"] = v["mktcap"] / f["annual_sales"]
            if any(v[k] is not None for k in ("per", "pbr", "psr")):
                v["src"].append("J-Quants")

    if allow_yf and any(v[k] is None for k in ("mktcap", "per", "pbr", "psr")):
        info = fetch_yf_info(code)
        if info:
            used = False
            if v["mktcap"] is None and info.get("marketCap"):
                v["mktcap"] = float(info["marketCap"]); used = True
            if v["per"] is None:
                per = info.get("trailingPE") or info.get("forwardPE")
                if per:
                    v["per"] = float(per); used = True
            if v["pbr"] is None and info.get("priceToBook"):
                v["pbr"] = float(info["priceToBook"]); used = True
            if v["psr"] is None and info.get("priceToSalesTrailing12Months"):
                v["psr"] = float(info["priceToSalesTrailing12Months"]); used = True
            if v["psr"] is None and v["mktcap"] and info.get("totalRevenue"):
                v["psr"] = v["mktcap"] / float(info["totalRevenue"]); used = True
            if used:
                v["src"].append("yfinance")

    return v


# ============================================================
# Claude によるファンダメンタル分析（Anthropic API 直叩き）
# ============================================================

CLAUDE_SYSTEM = (
    "あなたは日本株のファンダメンタル分析アシスタントです。"
    "与えられた実データを根拠に、簡潔で具体的な分析を日本語で書いてください。"
    "推測と事実を明確に分け、根拠が薄い部分は「不明」と書いてください。"
    "投資助言ではなく判断材料の整理であることを踏まえ、断定的な売買推奨はしないでください。"
)


def build_claude_prompt(company_name, code, facts):
    lines = [f"- {k}: {v}" for k, v in facts.items() if v not in (None, "", "-")]
    return (
        f"{company_name}（証券コード {code}、東京証券取引所上場）を分析してください。\n\n"
        f"スクリーニングで取得した実データ:\n" + "\n".join(lines) + "\n\n"
        "以下の構成で出力してください。\n"
        "1. **事業と収益構造**（3行程度）\n"
        "2. **バリュエーション評価** — 上のPER/PBR/PSRを同業他社水準と比べてどう見るか\n"
        "3. **株価下落・安値更新の背景** — 直近のニュースや業績修正など、判明する範囲で\n"
        "4. **注目すべきリスク**（箇条書き3点）\n"
        "5. **確認すべき次の情報**（箇条書き2〜3点）\n"
    )


def analyze_with_claude(company_name, code, facts, api_key, use_web_search=True):
    """Anthropic APIを呼んで分析テキストを返す。(text, error)"""
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2000,
        "system": CLAUDE_SYSTEM,
        "messages": [{"role": "user", "content": build_claude_prompt(company_name, code, facts)}],
    }
    if use_web_search:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]

    try:
        res = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            data=json.dumps(payload),
            timeout=180,
        )
    except Exception as e:
        return None, f"通信エラー: {e}"

    if res.status_code != 200:
        return None, f"HTTP {res.status_code}: {res.text[:300]}"

    data = res.json()
    # web_searchを使うと content に検索ブロックが混ざるので、type=="text" だけ拾う
    texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    text = "\n\n".join(t for t in texts if t.strip())
    return (text or None), (None if text else "空のレスポンスが返りました。")


def claude_web_url(company_name, code, facts):
    """APIキーが無い場合の代替。claude.ai をプロンプト付きで開く。"""
    q = urllib.parse.quote(build_claude_prompt(company_name, code, facts))
    return f"https://claude.ai/new?q={q}"


# ============================================================
# スクリーニング判定
# ============================================================

def compute_from_date(lookback_days, decline_lookback_days, use_ytd_low, need_decline_data):
    today = date.today()
    cands = [today - timedelta(days=int(lookback_days * 2.5) + 10)]
    if use_ytd_low:
        cands.append(date(today.year, 1, 1))
    if need_decline_data:
        cands.append(today - timedelta(days=decline_lookback_days))
    return min(cands)


def trim_today(hist, exclude_today):
    """J-Quantsは大引け後更新、yfinanceは場中の途中経過が入るため揃える。"""
    if not exclude_today or hist is None or hist.empty:
        return hist
    return hist[hist.index < pd.Timestamp(date.today())]


def screen_hist(code, hist, min_avg_volume, lookback_days, decline_threshold_pct,
                decline_lookback_days, use_ytd_low, use_decline, use_ore_teki,
                price_min, price_max):
    if hist is None or hist.empty:
        return None

    today = date.today()
    jan1 = date(today.year, 1, 1)
    decline_from = today - timedelta(days=decline_lookback_days)
    need_decline_data = use_decline or use_ore_teki

    if len(hist) < lookback_days:
        return None
    avg_volume = hist['Volume'].tail(lookback_days).mean()
    if pd.isna(avg_volume) or avg_volume < min_avg_volume:
        return None

    ytd_low_hit = decline_pct = latest_close = None

    if use_ytd_low:
        ytd_hist = hist[hist.index.date >= jan1]
        if len(ytd_hist) < 2:
            return None
        ytd_low, latest_low = ytd_hist['Low'].min(), ytd_hist['Low'].iloc[-1]
        if pd.isna(ytd_low) or pd.isna(latest_low):
            return None
        ytd_low_hit = latest_low <= ytd_low
        if not ytd_low_hit:
            return None

    if need_decline_data:
        if 'Close' not in hist.columns:
            return None
        recent_hist = hist[hist.index.date >= decline_from]
        if len(recent_hist) < 2:
            return None
        recent_high, latest_close = recent_hist['High'].max(), hist['Close'].iloc[-1]
        if pd.isna(recent_high) or pd.isna(latest_close) or recent_high <= 0:
            return None
        decline_pct = (recent_high - latest_close) / recent_high * 100
        if decline_pct < decline_threshold_pct:
            return None

    if use_ore_teki:
        if latest_close is None:
            if 'Close' not in hist.columns:
                return None
            latest_close = hist['Close'].iloc[-1]
        if pd.isna(latest_close) or not (price_min <= latest_close <= price_max):
            return None

    if latest_close is None and 'Close' in hist.columns:
        latest_close = hist['Close'].iloc[-1]

    mktcap = None
    if 'MktCap' in hist.columns:
        s = hist['MktCap'].dropna()
        if len(s):
            mktcap = float(s.iloc[-1]) * 1e6  # 百万円 → 円

    return {
        "code": code,
        "avg_volume": avg_volume,
        "ytd_low_hit": ytd_low_hit,
        "decline_pct": decline_pct,
        "latest_close": latest_close,
        "mktcap": mktcap,
    }


def send_discord_notify(msg):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
        except Exception:
            pass


def tradingview_symbol_url(code):
    return f"https://jp.tradingview.com/symbols/TSE-{code}/"


# ============================================================
# 表示コンポーネント
# ============================================================

def render_valuation_row(val):
    """PER / PBR / PSR / 時価総額 を横並びで見やすく表示する。"""
    if not val:
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("PER", format_ratio(val.get("per")))
    c2.metric("PBR", format_ratio(val.get("pbr")))
    c3.metric("PSR", format_ratio(val.get("psr")))
    notes = []
    if val.get("src"):
        notes.append("出所: " + " + ".join(val["src"]))
    if val.get("sales_src"):
        notes.append(f"PSR売上: {val['sales_src']}")
    if val.get("disc_date"):
        notes.append(f"開示日: {val['disc_date']}")
    if val.get("roe") is not None:
        notes.append(f"ROE: {val['roe'] * 100:,.1f}%")
    if notes:
        st.caption(" ｜ ".join(notes))


def render_claude_free(company_name, code, facts, key_prefix):
    """
    API料金のかからないルート。
    claude.ai をプロンプト付きで開く（契約プラン内で使えるので従量課金なし）。
    URLが長くなりすぎて切れる環境向けに、コピー用のプロンプトも併記する。
    """
    prompt = build_claude_prompt(company_name, code, facts)
    st.link_button(
        "🧠 Claudeで分析（claude.aiを開く・追加料金なし）",
        claude_web_url(company_name, code, facts),
        use_container_width=True,
        key=f"{key_prefix}_claude_link_{code}",
    )
    with st.expander("📋 うまく開かない場合：この質問文をコピーしてClaudeに貼り付け"):
        st.code(prompt, language="markdown")


def render_claude_panel(company_name, code, facts, key_prefix):
    """Geminiボタンの置き換え。無料ルートとAPIルートを切り替える。"""
    if not ANTHROPIC_API_KEY or st.session_state.get("free_mode", True):
        render_claude_free(company_name, code, facts, key_prefix)
        if ANTHROPIC_API_KEY:
            st.caption("サイドバーの「Claude分析をアプリ内で実行」をONにすると画面内で完結します（API課金あり）。")
        return

    state_key = f"claude_result_{key_prefix}_{code}"
    if st.button("🧠 Claudeでファンダメンタル分析", key=f"{key_prefix}_claude_btn_{code}",
                 use_container_width=True):
        with st.spinner("Claudeが分析中（web検索あり・30〜60秒）..."):
            text, err = analyze_with_claude(company_name, code, facts, ANTHROPIC_API_KEY)
        st.session_state[state_key] = text if text else f"分析に失敗しました: {err}"

    if st.session_state.get(state_key):
        with st.container(border=True):
            st.markdown(st.session_state[state_key])


def render_company_card(company_name, code, key_prefix, caption_parts=None,
                        mktcap=None, val=None, facts=None):
    tv_url = tradingview_symbol_url(code)
    with st.container(border=True):
        mc_text = format_mktcap(mktcap)
        st.markdown(
            f"#### [{company_name}]({tv_url}) "
            f"<span style='font-size:0.8em; color:gray;'>({code})</span> "
            f"<span style='font-size:0.75em; background:#eef2f7; color:#334; "
            f"padding:2px 8px; border-radius:10px; margin-left:6px;'>時価総額 {mc_text}</span>",
            unsafe_allow_html=True,
        )
        if caption_parts:
            st.caption(" ｜ ".join(caption_parts))

        if val:
            render_valuation_row(val)

        st.caption("👆 企業名をタップするとTradingView（日本語）が開きます。")
        render_claude_panel(company_name, code, facts or {}, key_prefix)


# ============================================================
# データ読み込み・サイドバー
# ============================================================

df_jpx = load_jpx_data()
market_options = sector_options = ["すべて"]
if not df_jpx.empty:
    df_jpx['コード_str'] = df_jpx['コード'].apply(normalize_code)
    market_options = ["すべて"] + sorted(df_jpx['市場・商品区分'].unique().tolist())
    sector_options = ["すべて"] + sorted(df_jpx['33業種区分'].unique().tolist())

st.sidebar.header("⚙️ データソース設定")
st.session_state.data_source = st.sidebar.radio(
    "株価データの取得元", ["yfinance", "J-Quants"],
    index=0 if st.session_state.data_source == "yfinance" else 1,
)

jquants_api_key = JQUANTS_API_KEY_SECRET
if st.session_state.data_source == "J-Quants":
    if JQUANTS_API_KEY_SECRET:
        st.sidebar.success("secretsのJ-Quants APIキーを使用します。")
    else:
        jquants_api_key = st.sidebar.text_input("J-Quants APIキー", type="password")
        if not jquants_api_key:
            st.sidebar.warning("APIキーが未入力です。")

st.session_state.exclude_today = st.sidebar.checkbox(
    "当日分を除外して判定する", value=st.session_state.exclude_today,
    help="J-Quantsは大引け後更新、yfinanceは場中の途中経過が入るため、揃えたい場合はON。",
)
st.session_state.yf_fallback = st.sidebar.checkbox(
    "不足指標をyfinanceで補完する", value=st.session_state.yf_fallback,
    help="J-Quantsで埋まらなかったPER/PBR/PSR/時価総額をyfinanceから補います（やや遅くなります）。",
)

st.sidebar.divider()
st.sidebar.caption(
    f"J-Quantsは日付指定で全銘柄をまとめて取得（{JQ_RATE_LIMIT_PER_MIN}req/分で平準化）。"
    "プランを上げたら JQ_RATE_LIMIT_PER_MIN も上げてください。"
)
if ANTHROPIC_API_KEY:
    st.session_state.free_mode = not st.sidebar.checkbox(
        "Claude分析をアプリ内で実行する（API課金あり）",
        value=not st.session_state.get("free_mode", True),
        help="OFFのままなら claude.ai を開くだけなので追加料金はかかりません。",
    )
    if st.session_state.free_mode:
        st.sidebar.info("Claude分析: 無料モード（claude.aiを開く）")
    else:
        st.sidebar.warning(f"Claude分析: アプリ内実行（{ANTHROPIC_MODEL} / 従量課金）")
else:
    st.session_state.free_mode = True
    st.sidebar.info("Claude分析: 無料モード（claude.aiを開く）")

# ============================================================
# メイン
# ============================================================

st.title("📈 株式スクリーニングダッシュボード")

with st.container(border=True):
    st.markdown("##### 🎛️ フィルターバー")
    f1, f2 = st.columns(2)
    with f1:
        st.session_state.market_filter = st.selectbox(
            "市場区分", market_options,
            index=market_options.index(st.session_state.market_filter)
            if st.session_state.market_filter in market_options else 0)
    with f2:
        st.session_state.sector_filter = st.selectbox(
            "業種", sector_options,
            index=sector_options.index(st.session_state.sector_filter)
            if st.session_state.sector_filter in sector_options else 0)

    st.markdown("---")
    st.markdown("###### 📉 スクリーニング条件")
    c1, c2 = st.columns(2)
    with c1:
        st.session_state.use_ytd_low = st.checkbox(
            "年初来安値更新（当日の安値が年初来安値を更新）", value=st.session_state.use_ytd_low)
    with c2:
        st.session_state.use_decline = st.checkbox(
            f"直近3ヶ月の高値からの下落率が約{DECLINE_THRESHOLD_PCT:.0f}%以上",
            value=st.session_state.use_decline)
    st.session_state.use_ore_teki = st.checkbox(
        f"🎯 俺的株（下落率約{DECLINE_THRESHOLD_PCT:.0f}%以上 かつ 株価"
        f"{ORE_TEKI_PRICE_MIN:,.0f}〜{ORE_TEKI_PRICE_MAX:,.0f}円）",
        value=st.session_state.use_ore_teki)

    search_btn = st.button("🚀 スクリーニングを実行する", type="primary", use_container_width=True)

st.markdown("---")
tab_screen, tab_list = st.tabs(["🔍 スクリーニング結果", "📋 全銘柄一覧"])

# ------------------------------------------------------------
# タブ1
# ------------------------------------------------------------
with tab_screen:
    if search_btn and not df_jpx.empty:
        if st.session_state.data_source == "J-Quants" and not jquants_api_key:
            st.error("J-Quantsを選択している場合はAPIキーが必要です。")
        elif not any([st.session_state.use_ytd_low, st.session_state.use_decline,
                      st.session_state.use_ore_teki]):
            st.warning("⚠️ いずれか1つ以上の条件にチェックを入れてください。")
        else:
            target_df = df_jpx.copy()
            if st.session_state.market_filter != "すべて":
                target_df = target_df[target_df['市場・商品区分'] == st.session_state.market_filter]
            if st.session_state.sector_filter != "すべて":
                target_df = target_df[target_df['33業種区分'] == st.session_state.sector_filter]
            codes = target_df['コード_str'].tolist()

            if not codes:
                st.warning("⚠️ 条件に合致する銘柄がありませんでした。")
            else:
                need_decline = st.session_state.use_decline or st.session_state.use_ore_teki
                from_dt = compute_from_date(LOOKBACK_DAYS, DECLINE_LOOKBACK_DAYS,
                                            st.session_state.use_ytd_low, need_decline)
                from_str = from_dt.strftime("%Y-%m-%d")
                to_str = date.today().strftime("%Y-%m-%d")

                kw = dict(min_avg_volume=MIN_AVG_VOLUME, lookback_days=LOOKBACK_DAYS,
                          decline_threshold_pct=DECLINE_THRESHOLD_PCT,
                          decline_lookback_days=DECLINE_LOOKBACK_DAYS,
                          use_ytd_low=st.session_state.use_ytd_low,
                          use_decline=st.session_state.use_decline,
                          use_ore_teki=st.session_state.use_ore_teki,
                          price_min=ORE_TEKI_PRICE_MIN, price_max=ORE_TEKI_PRICE_MAX)

                screen_results = []

                if st.session_state.data_source == "J-Quants":
                    ptext = "J-Quantsから全銘柄の日足を取得中..."
                    bar = st.progress(0, text=ptext)
                    panel, ferrs = fetch_jquants_panel(
                        from_str, to_str, jquants_api_key,
                        _progress=lambda r, d, t: bar.progress(min(r, 1.0), text=f"{ptext} ({d}/{t}営業日)"))
                    bar.empty()

                    if ferrs:
                        st.error(f"⚠️ J-Quantsの取得で{len(ferrs)}件のエラーが発生しました。")
                        with st.expander("エラー詳細"):
                            for e in ferrs[:30]:
                                st.text(e)

                    if not panel:
                        st.error("J-Quantsからデータを取得できませんでした。APIキー・プラン・レートリミットを確認してください。")
                    else:
                        st.caption(f"取得できた銘柄数: {len(panel)}件")
                        for code in codes:
                            hist = trim_today(panel.get(code), st.session_state.exclude_today)
                            r = screen_hist(code, hist, **kw)
                            if r:
                                screen_results.append(r)
                else:
                    ptext = "銘柄データを解析中（yfinance）..."
                    bar = st.progress(0, text=ptext)

                    def _one(code):
                        h = trim_today(fetch_yfinance_history(code, from_str, to_str),
                                       st.session_state.exclude_today)
                        return screen_hist(code, h, **kw)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                        futs = {ex.submit(_one, c): c for c in codes}
                        for i, f in enumerate(concurrent.futures.as_completed(futs)):
                            r = f.result()
                            if r:
                                screen_results.append(r)
                            bar.progress((i + 1) / len(codes), text=f"{ptext} ({i+1}/{len(codes)})")
                    bar.empty()

                m1, m2 = st.columns(2)
                m1.metric("① 対象銘柄数", f"{len(codes)} 件")
                m2.metric("② 条件クリア", f"{len(screen_results)} 件")

                final_results = []
                for res in screen_results:
                    match = target_df[target_df['コード_str'] == res["code"]]
                    if match.empty:
                        continue
                    row = match.iloc[0]
                    final_results.append({
                        "コード": res["code"], "会社名": row['銘柄名'],
                        "市場": row['市場・商品区分'], "業種": row['33業種区分'],
                        "規模カテゴリ": row.get('規模カテゴリ'),
                        "平均出来高 (株)": int(round(res["avg_volume"])),
                        "年初来安値更新": res.get("ytd_low_hit"),
                        "下落率 (%)": round(res["decline_pct"], 1) if res.get("decline_pct") is not None else "-",
                        "現在値 (円)": round(res["latest_close"], 1) if res.get("latest_close") is not None else "-",
                        "_mktcap": res.get("mktcap"),
                        "_close": res.get("latest_close"),
                    })

                # バリュエーション（時価総額・PER・PBR・PSR）をまとめて取得
                if final_results:
                    with st.spinner(f"バリュエーション指標を取得中（{len(final_results)}銘柄）..."):
                        for r in final_results:
                            r["_val"] = get_valuation(
                                r["コード"], st.session_state.data_source, jquants_api_key,
                                mktcap_yen=r["_mktcap"], close=r["_close"],
                                allow_yf=st.session_state.yf_fallback)
                            if r["_mktcap"] is None:
                                r["_mktcap"] = r["_val"].get("mktcap")

                for r in final_results:
                    v = r.get("_val", {})
                    parts = [f"平均出来高: {r['平均出来高 (株)']:,}株",
                             f"時価総額: {format_mktcap(r['_mktcap'])}"]
                    if r["年初来安値更新"] is not None:
                        parts.append("年初来安値更新: 該当")
                    if r['下落率 (%)'] != "-":
                        parts.append(f"下落率: {r['下落率 (%)']}%")
                    if r['現在値 (円)'] != "-":
                        parts.append(f"現在値: {r['現在値 (円)']}円")
                    parts.append(f"PER: {format_ratio(v.get('per'))} / PBR: {format_ratio(v.get('pbr'))} "
                                 f"/ PSR: {format_ratio(v.get('psr'))}")
                    send_discord_notify(
                        f"【スクリーニングヒット】\n{r['会社名']} ({r['コード']})\n" + " ｜ ".join(parts))

                st.session_state.last_screen_results = final_results
                st.session_state.last_screen_counts = (len(codes), len(screen_results))
                st.session_state.last_screen_conditions = (
                    st.session_state.use_ytd_low, st.session_state.use_decline,
                    st.session_state.use_ore_teki)

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

            display_results = final_results
            if any(r.get("規模カテゴリ") for r in final_results):
                smap = {"すべて": None, "大型株（TOPIX100）": "大型株",
                        "中型株（TOPIX Mid400）": "中型株", "小型株（TOPIX Small）": "小型株"}
                sopt = st.radio("規模区分で絞り込み", list(smap.keys()), horizontal=True,
                                key="screen_size_filter")
                if smap[sopt]:
                    display_results = [r for r in final_results if r.get("規模カテゴリ") == smap[sopt]]

            sort_key = st.selectbox("並び替え", ["下落率が大きい順", "時価総額が大きい順",
                                                 "時価総額が小さい順", "PBRが低い順", "PSRが低い順"],
                                    key="screen_sort")

            def _sk(r):
                v = r.get("_val", {})
                if sort_key == "下落率が大きい順":
                    return -(r['下落率 (%)'] if r['下落率 (%)'] != "-" else -1)
                if sort_key == "時価総額が大きい順":
                    return -(r.get("_mktcap") or 0)
                if sort_key == "時価総額が小さい順":
                    return r.get("_mktcap") or float("inf")
                if sort_key == "PBRが低い順":
                    return v.get("pbr") or float("inf")
                return v.get("psr") or float("inf")

            display_results = sorted(display_results, key=_sk)
            st.caption(f"表示件数: {len(display_results)}件")

            used_ytd, used_dec, used_ore = st.session_state.get(
                "last_screen_conditions", (True, True, False))

            def render_result_card(res):
                v = res.get("_val", {})
                caps = [f"市場: {res['市場']}", f"業種: {res['業種']}"]
                if res.get("規模カテゴリ"):
                    caps.append(f"規模: {res['規模カテゴリ']}")
                caps.append(f"直近{LOOKBACK_DAYS}日平均出来高: {res['平均出来高 (株)']:,}株")
                if used_ytd:
                    caps.append("年初来安値更新: 該当")
                if (used_dec or used_ore) and res['下落率 (%)'] != "-":
                    caps.append(f"3ヶ月高値からの下落率: {res['下落率 (%)']}%")
                if res['現在値 (円)'] != "-":
                    caps.append(f"現在値: {res['現在値 (円)']}円")

                facts = {
                    "市場": res['市場'], "業種": res['業種'],
                    "現在値": f"{res['現在値 (円)']}円",
                    "時価総額": format_mktcap(res.get("_mktcap")),
                    "PER": format_ratio(v.get("per")), "PBR": format_ratio(v.get("pbr")),
                    "PSR": format_ratio(v.get("psr")),
                    "直近3ヶ月高値からの下落率": f"{res['下落率 (%)']}%" if res['下落率 (%)'] != "-" else None,
                    "年初来安値更新": "該当" if used_ytd else None,
                    "直近20日平均出来高": f"{res['平均出来高 (株)']:,}株",
                    "最新決算開示日": v.get("disc_date"),
                }
                render_company_card(res["会社名"], res["コード"], key_prefix="screen",
                                    caption_parts=caps, mktcap=res.get("_mktcap"),
                                    val=v, facts=facts)

            sectors = sorted({r["業種"] for r in display_results if r.get("業種")})
            if st.checkbox("🏭 業種ごとにグループ表示する", value=True, key="screen_group") and sectors:
                for sec in sectors:
                    rows = [r for r in display_results if r.get("業種") == sec]
                    with st.expander(f"🏭 {sec}（{len(rows)}件）", expanded=True):
                        for r in rows:
                            render_result_card(r)
            else:
                for r in display_results:
                    render_result_card(r)
        else:
            st.warning("⚠️ 指定した条件をクリアした銘柄はありませんでした。")
    elif not search_btn:
        st.info("👆 条件を設定して「スクリーニングを実行する」を押してください。")

# ------------------------------------------------------------
# タブ2
# ------------------------------------------------------------
with tab_list:
    if df_jpx.empty:
        st.info("銘柄データが読み込まれていません。")
    else:
        asof = (date.today() - timedelta(days=1)) if st.session_state.exclude_today else date.today()
        snapshot = {}
        if st.session_state.data_source == "J-Quants" and jquants_api_key:
            with st.spinner("全銘柄の時価総額を取得中（1リクエスト）..."):
                snapshot = fetch_jquants_snapshot(jquants_api_key, asof.strftime("%Y-%m-%d"))

        st.markdown("全銘柄の一覧です。銘柄コードを入力して検索するか、下のリストから確認してください。")
        st.markdown("---")

        search_code_input = st.text_input("銘柄コードで検索（例: 4792, 7203）", value="")
        if search_code_input:
            code = search_code_input.strip()
            trow = df_jpx[df_jpx['コード_str'] == code]
            if trow.empty:
                st.error("指定されたコードが見つかりませんでした。")
            else:
                r0 = trow.iloc[0]
                today = date.today()
                jan1 = date(today.year, 1, 1)
                from_s, to_s = jan1.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

                with st.spinner("株価情報を取得中..."):
                    if st.session_state.data_source == "J-Quants" and jquants_api_key:
                        hist = fetch_jquants_single(code, from_s, to_s, jquants_api_key)
                    else:
                        hist = fetch_yfinance_history(code, from_s, to_s)
                    hist = trim_today(hist, st.session_state.exclude_today)

                    close = mktcap = ytd_low = avg_vol = None
                    is_ytd_low = False
                    if hist is not None and not hist.empty:
                        close = float(hist['Close'].iloc[-1])
                        ytd_low = float(hist['Low'].min())
                        is_ytd_low = float(hist['Low'].iloc[-1]) <= ytd_low
                        avg_vol = hist['Volume'].tail(20).mean()
                        if 'MktCap' in hist.columns:
                            s = hist['MktCap'].dropna()
                            if len(s):
                                mktcap = float(s.iloc[-1]) * 1e6
                    if mktcap is None:
                        mktcap = (snapshot.get(code) or {}).get("mktcap")

                    val = get_valuation(code, st.session_state.data_source, jquants_api_key,
                                        mktcap_yen=mktcap, close=close,
                                        allow_yf=st.session_state.yf_fallback)
                    if mktcap is None:
                        mktcap = val.get("mktcap")

                caps = [f"市場: {r0['市場・商品区分']}", f"業種: {r0['33業種区分']}"]
                if ytd_low is not None:
                    caps.append(f"年初来安値: {ytd_low:,.1f}円")
                    caps.append(f"年初来安値更新: {'✅ 更新中' if is_ytd_low else '－'}")
                if avg_vol is not None and not pd.isna(avg_vol):
                    caps.append(f"直近20日平均出来高: {int(round(avg_vol)):,}株")

                facts = {
                    "市場": r0['市場・商品区分'], "業種": r0['33業種区分'],
                    "現在値": f"{close:,.1f}円" if close else None,
                    "時価総額": format_mktcap(mktcap),
                    "PER": format_ratio(val.get("per")), "PBR": format_ratio(val.get("pbr")),
                    "PSR": format_ratio(val.get("psr")),
                    "年初来安値": f"{ytd_low:,.1f}円" if ytd_low else None,
                    "年初来安値更新": "該当" if is_ytd_low else "非該当",
                    "最新決算開示日": val.get("disc_date"),
                }
                render_company_card(r0['銘柄名'], code, key_prefix="search",
                                    caption_parts=caps, mktcap=mktcap, val=val, facts=facts)

        st.markdown("---")
        st.markdown("###### 🏷️ 規模別一覧（TOPIXの規模区分に基づく）")

        if '規模カテゴリ' not in df_jpx.columns:
            st.warning("「規模区分」列が無いため規模別一覧は表示できません。data_j.xlsを最新版に更新してください。")
        else:
            smap = {"大型株（TOPIX100）": "大型株", "中型株（TOPIX Mid400）": "中型株",
                    "小型株（TOPIX Small）": "小型株"}
            sopt = st.radio("規模区分を選択", list(smap.keys()), horizontal=True)
            size_df = df_jpx[df_jpx['規模カテゴリ'] == smap[sopt]]

            lsearch = st.text_input("銘柄名で絞り込み（任意）", value="", key="size_list_search")
            if lsearch:
                size_df = size_df[size_df['銘柄名'].str.contains(lsearch, na=False)]

            st.caption(f"該当銘柄数: {len(size_df)}件")
            display_df = size_df.head(50)
            if len(size_df) > 50:
                st.caption("※ 先頭50件を表示しています。")

            show_val = st.checkbox(
                "💰 PER / PBR / PSR も表示する（銘柄ごとにAPIを叩くため時間がかかります）",
                value=False, key=f"sizelist_show_val_{smap[sopt]}")

            val_map = {}
            if show_val and not display_df.empty:
                codes_in_view = display_df['コード_str'].tolist()
                with st.spinner(f"バリュエーション指標を取得中（{len(codes_in_view)}銘柄）..."):
                    for c in codes_in_view:
                        snap = snapshot.get(c) or {}
                        val_map[c] = get_valuation(
                            c, st.session_state.data_source, jquants_api_key,
                            mktcap_yen=snap.get("mktcap"), close=snap.get("close"),
                            allow_yf=st.session_state.yf_fallback)

            for _, row in display_df.iterrows():
                cs = row['コード_str']
                snap = snapshot.get(cs) or {}
                v = val_map.get(cs)
                mc = snap.get("mktcap") or (v.get("mktcap") if v else None)
                caps = [f"市場: {row['市場・商品区分']}", f"業種: {row['33業種区分']}"]
                facts = {
                    "市場": row['市場・商品区分'], "業種": row['33業種区分'],
                    "時価総額": format_mktcap(mc),
                    "PER": format_ratio(v.get("per")) if v else None,
                    "PBR": format_ratio(v.get("pbr")) if v else None,
                    "PSR": format_ratio(v.get("psr")) if v else None,
                }
                render_company_card(row['銘柄名'], cs, key_prefix=f"sizelist_{smap[sopt]}",
                                    caption_parts=caps, mktcap=mc, val=v, facts=facts)
