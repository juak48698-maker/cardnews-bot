import os
import io
import re
import json
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageOps

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

SEED_KEYWORDS = ["주식", "미국주식", "금리", "환율", "반도체", "비트코인", "부동산", "코스피"]

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

HANDLE = "@로투파"

W, H = 1080, 1350  # 4:5 세로형
SAFE_PAD = 90

WHITE = (245, 246, 248)
BRAND_GREEN = (57, 255, 20)  # 강조 색상

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

PENDING_PHOTOS = {}
ACTIVE_JOBS = {}  # chat_id -> {"cancelled": bool}
PROCESSED_UPDATE_IDS = deque(maxlen=2000)  # 텔레그램 재전송(같은 update_id) 중복 처리 방지
PHOTO_LOCK = threading.Lock()  # used_photo_ids 동시접근 보호 (병렬 사진검색용)
PENDING_TOPICS = {}  # chat_id -> ["/추천"으로 나온 주제 문자열 리스트] (인기도 순)
SELECTED_TOPIC = {}  # chat_id -> {"question","source_text","verified"} (콘텐츠 종류 선택 전까지 임시 보관)


def font(path, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, path), size)


F_TITLE = "IBMPlexSansKR-Bold.ttf"
F_BODY = "IBMPlexSansKR-Regular.ttf"
F_MONO = "IBMPlexMono-Medium.ttf"


def wrap_text(draw, text, fnt, max_width):
    lines = []
    current = ""
    for ch in text:
        test = current + ch
        w = draw.textlength(test, font=fnt)
        if w > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines


def strip_emoji(text):
    """폰트가 지원하지 않는 이모지 등을 안전하게 제거 (AI가 실수로 넣어도 깨진 박스 방지)"""
    pattern = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0001F1E6-\U0001F1FF"
        "\U00002B00-\U00002BFF"
        "\U0001F000-\U0001F0FF"
        "\U0000FE0F"
        "]+", flags=re.UNICODE)
    return pattern.sub("", text).strip()


def flatten_text(text):
    """줄바꿈이 섞여있으면 draw.textlength()가 에러나므로, 렌더링 전에 한 줄로 평탄화"""
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ").replace("\r", " ")).strip()


def apply_gradient(img, top_alpha, bottom_alpha):
    """이미지 전체에 위→아래로 어두워지는 그라데이션(오버레이) 적용"""
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(H):
        alpha = int(top_alpha + (bottom_alpha - top_alpha) * (y / H))
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))


def draw_watermark(draw):
    """배경 밝기와 상관없이 항상 잘 보이도록 반투명 검은 배경 위에 워터마크 표시"""
    text_w = draw.textlength(HANDLE, font=font(F_BODY, 24))
    pad_x, pad_y = 16, 10
    x0, y0 = SAFE_PAD - pad_x, 60 - pad_y
    x1, y1 = SAFE_PAD + text_w + pad_x, 60 + 24 + pad_y
    draw.rounded_rectangle([x0, y0, x1, y1], radius=8, fill=(0, 0, 0, 130))
    draw.text((SAFE_PAD, 60), HANDLE, font=font(F_BODY, 24), fill=WHITE)


def draw_title_marker(draw, x, y, size):
    """이모지 대신 폰트에 항상 있는 브랜드컬러 사각 마커로 제목 앞부분 표시 (폴백용)"""
    m = int(size * 0.32)
    top = y + int(size * 0.22)
    draw.rounded_rectangle([x, top, x + m, top + m], radius=4, fill=BRAND_GREEN)
    return x + m + int(size * 0.28)


def fetch_emoji_image(emoji_char, size=96):
    """Twemoji CDN에서 컬러 이모지 이미지를 가져옴. 실패하면 None 반환 (호출부에서 폴백 처리)"""
    if not emoji_char:
        return None
    try:
        cps = [c for c in emoji_char if ord(c) not in (0xFE0F, 0x200D) and not (0xD800 <= ord(c) <= 0xDFFF)]
        if not cps:
            return None
        if len(cps) == 1:
            codepoint = f"{ord(cps[0]):x}"
        else:
            codepoint = "-".join(f"{ord(c):x}" for c in cps)
        url = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{codepoint}.png"
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


# ============ 볼드(**강조**) 문단 줄바꿈 + 가운데 정렬 렌더링 ============
def tokenize_bold(text):
    parts = re.split(r"(\*\*.*?\*\*)", text)
    tokens = []
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**"):
            for w in p[2:-2].split(" "):
                if w:
                    tokens.append((w, True))
        else:
            for w in p.split(" "):
                if w:
                    tokens.append((w, False))
    return tokens


def wrap_mixed_tokens(draw, tokens, size, max_width):
    space_w = draw.textlength(" ", font=font(F_BODY, size))
    lines, current, current_w = [], [], 0
    for word, bold in tokens:
        fnt = font(F_TITLE, size) if bold else font(F_BODY, size)
        w = draw.textlength(word, font=fnt)
        add_w = w + (space_w if current else 0)
        if current_w + add_w > max_width and current:
            lines.append(current)
            current, current_w = [(word, bold)], w
        else:
            current.append((word, bold))
            current_w += add_w
    if current:
        lines.append(current)
    return lines


def draw_centered_mixed_line(draw, y, line, size, base_color, accent_color=None):
    accent_color = accent_color or base_color
    space_w = draw.textlength(" ", font=font(F_BODY, size))
    widths = []
    total_w = 0
    for i, (word, bold) in enumerate(line):
        fnt = font(F_TITLE, size) if bold else font(F_BODY, size)
        w = draw.textlength(word, font=fnt)
        widths.append(w)
        total_w += w + (space_w if i > 0 else 0)
    x = (W - total_w) / 2
    for (word, bold), w in zip(line, widths):
        fnt = font(F_TITLE, size) if bold else font(F_BODY, size)
        draw.text((x, y), word, font=fnt, fill=(accent_color if bold else base_color))
        x += w + space_w
    return y


# ============ AI로 원문 → 카드 구조 변환 ============
def ai_generate_5slides(source_text):
    """5슬라이드 공식(후킹→팩트→반전→예고→CTA)을 고정 구조로 카드+캡션 생성"""
    prompt = f"""너는 인스타그램 금융/재테크 카드뉴스 전문 카피라이터야.
아래 "5슬라이드 공식"을 정확히 따라서 카드뉴스와 캡션을 작성해줘.
이 공식은 공포/반전/궁금증 유발 구조로, 인게이지먼트(댓글·팔로우 전환)를 극대화하는 데 최적화되어 있어.

[참고 자료]
{source_text}

### 구조 규칙 (반드시 5장 고정, 순서도 이대로)

[슬라이드 1] 후킹 (표지)
- 두 줄 모두 최대 7어절 이내, 초등학생도 이해할 쉬운 단어만 사용 (전문용어·복문 절대 금지 — "지정학적", "펀더멘털" 같은 단어는 본문에서만 써도 됨)
- 완전한 문장일 필요 없음. 짧고 임팩트 있는 구(句) 형태 허용
- 숫자는 반드시 포함하되 단순하게 (예: "17%대 폭락"보다 "-17%")
- 패턴: 1줄=사실/숫자 툭 던지기, 2줄=반전 또는 질문으로 궁금증 유발
  예) 1줄 "하루 만에 -6%" / 2줄 "근데 다들 웃었다?"
  예) 1줄 "월가가 놀란 이유" / 2줄 "숫자 하나 때문"
- 배경은 위기감 도는 톤(어두운 톤, 하락/긴장 이미지)

[슬라이드 2] 팩트/증거 제시
- 실제 데이터/뉴스 헤드라인 톤 문구 + 위기를 뒷받침하는 2차 팩트 한 줄
- 핵심 수치는 반드시 **볼드**로 감싸기

[슬라이드 3] 반전/의문 제기
- "핵심 질문은 사실 '~인가' 하는 거예요" 톤으로 진짜 쟁점 제시
- 모순되는 두 정보를 대비시키기 (예: "역대급 실적인데 왜 주가는 폭락했나?")
- 불안을 증폭시키는 문장으로 마무리

[슬라이드 4] 해결책 예고 — 절대 답을 주지 말 것
- "그럼 지금, 가장 먼저 무엇을 점검해야 할까요?" 톤의 질문형
- 체크리스트/포인트가 있다는 것만 예고하고, 구체적인 내용은 절대 공개하지 마

[슬라이드 5] CTA
- "게시글의 추가 내용이 궁금하다면?" 톤
- 정확히 "팔로우 후 댓글에 **이모티콘**을 남겨주시면" 패턴으로 유도해줘. 실제 이모지 문자를 넣지 말고 "이모티콘"이라는 단어 자체를 반드시 **볼드**로 감싸서 써 (body에서 렌더링될 때 자동으로 네온그린으로 강조됨)
- DM이나 자료 전송 방식은 카드 문구에 절대 언급하지 마, 팔로우+댓글 유도까지만

### 톤 규칙
- 문장은 짧고 단정적으로 (설명체 X, 선언체 O)
- 각 슬라이드는 다음 장이 궁금해지는 여운으로 마무리

[슬라이드4에서 예고한 체크리스트를 실제로 작성해줘 — 발송용 자료]
- 슬라이드4에서는 예고만 하고 카드에는 절대 내용을 공개하지 않지만, 이 체크리스트는 실제로 발송할 자료라서 진짜로 채워야 해
- 반드시 [참고 자료]에 있는 사실관계에서만 도출해, 참고자료에 없는 새로운 사실이나 수치는 절대 지어내지 마
- 5~7개 항목, 각 항목은 title(점검 포인트 제목, 짧게) / why(왜 중요한지, 참고자료의 어떤 사실과 연결되는지) / how(구체적으로 무엇을, 어떻게 확인하면 되는지)로 구성
- why와 how는 따로 라벨을 붙이지 않고 이어붙여도 하나의 자연스러운 문단처럼 읽히게 써줘 (예: "~라는 신호예요. 그러니 ~를 확인해보세요" 처럼 근거→행동 순으로 매끄럽게 연결)
- 특정 종목 매수/매도 지시, 목표가, 수익률 예측 같은 직접적 투자 지시는 절대 쓰지 마 (정보 제공까지만)

### 출력 (아래 JSON 형식으로만, 다른 설명 붙이지 마)
{{
  "cover": {{"line1": "슬라이드1 첫 줄", "line2": "슬라이드1 둘째 줄(강조)", "search_query": "위기감 도는 배경사진 영어검색어 2~4단어"}},
  "slides": [
    {{"emoji": "이모지1개", "title": "슬라이드2 헤드라인", "body": "팩트+2차팩트, 핵심수치는 **볼드**", "search_query": "데이터/차트 느낌 영어검색어"}},
    {{"emoji": "😰", "title": "슬라이드3 헤드라인(쟁점)", "body": "모순 대비 + 불안 증폭 마무리", "search_query": "영어검색어"}},
    {{"emoji": "🙌", "title": "슬라이드4 헤드라인(질문형)", "body": "체크리스트 있다는 예고만, 내용 절대 공개 금지", "search_query": "영어검색어"}},
    {{"emoji": "💬", "title": "슬라이드5 헤드라인", "body": "팔로우 후 댓글에 **이모티콘**을 남겨주시면 유도 문구 (DM/발송 언급 금지)", "search_query": "영어검색어"}}
  ],
  "caption": "질문 1줄 + 투표/댓글 유도, 짧게",
  "checklist": [
    {{"title": "점검 포인트 제목", "why": "왜 중요한지 (근거자료 사실 연결)", "how": "구체적으로 어떻게 확인하는지"}}
  ]
}}
title, body, line1, line2, caption 안에는 이모지 넣지 마 (emoji 필드에만 딱 1개씩).
"""
    return call_claude_json(prompt, max_tokens=2400)


def youtube_top_videos(query, max_results=4):
    """최근 48시간 내 업로드된 영상 중 조회수 높은 순으로 가져옴"""
    import datetime
    published_after = (datetime.datetime.utcnow() - datetime.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "q": query, "type": "video", "order": "viewCount",
        "publishedAfter": published_after, "maxResults": max_results,
        "regionCode": "KR", "relevanceLanguage": "ko", "key": YOUTUBE_API_KEY,
    }, timeout=15)
    r.raise_for_status()
    items = r.json().get("items", [])
    video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    if not video_ids:
        return []
    r2 = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
        "part": "snippet,statistics", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY,
    }, timeout=15)
    r2.raise_for_status()
    out = [{
        "title": v["snippet"]["title"],
        "views": int(v["statistics"].get("viewCount", 0)),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    } for v in r2.json().get("items", [])]
    out.sort(key=lambda x: x["views"], reverse=True)
    return out


def google_trends_rising(keyword):
    """해당 키워드와 관련해서 최근 48시간 내 급상승 중인 연관 검색어"""
    try:
        import datetime
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="ko-KR", tz=540)
        now = datetime.datetime.utcnow()
        start = now - datetime.timedelta(hours=48)
        # pytrends는 "now 7-d" 같은 프리셋 외에, 시간 단위 커스텀 범위("YYYY-MM-DDTHH YYYY-MM-DDTHH")도 지원함
        timeframe = f"{start.strftime('%Y-%m-%dT%H')} {now.strftime('%Y-%m-%dT%H')}"
        pytrends.build_payload([keyword], timeframe=timeframe, geo="KR")
        related = pytrends.related_queries()
        rising = related.get(keyword, {}).get("rising")
        if rising is None or rising.empty:
            return []
        return rising["query"].tolist()[:5]
    except Exception:
        return []


def collect_trend_data():
    """시드 키워드 중 일부를 뽑아 유튜브+구글트렌드 데이터를 텍스트로 모으고, 참고링크 목록도 같이 반환
    (인기도 순위를 매기기 위한 신호일 뿐, 사실관계 근거는 아님 — 팩트체크는 주제 선택 후 별도로 진행)"""
    import random
    picks = random.sample(SEED_KEYWORDS, k=min(4, len(SEED_KEYWORDS)))
    lines = []
    references = []  # [(라벨, url), ...]
    for kw in picks:
        try:
            for v in youtube_top_videos(kw, max_results=4):
                lines.append(f"[유튜브 인기/{kw}] {v['title']} (조회수 {v['views']:,})")
                references.append((f"▶ {v['title']} (조회수 {v['views']:,})", v["url"]))
        except Exception:
            pass
        for q in google_trends_rising(kw):
            lines.append(f"[구글트렌드 급상승/{kw}] {q}")
            import urllib.parse
            trend_url = "https://trends.google.co.kr/trends/explore?geo=KR&q=" + urllib.parse.quote(q)
            references.append((f"📈 '{q}' 검색 트렌드", trend_url))
    return "\n".join(lines), references


# ============ 뉴스기사 팩트체크 (선택된 주제 1개에 대해서만 실행) ============
def fetch_naver_news(query, display=3, max_age_hours=48):
    """네이버 뉴스 검색 API로 실제 국내 기사 목록(제목/요약/링크)을 가져옴 (무료)
    게시된 지 max_age_hours(기본 48시간)가 지난 기사는 제외함"""
    import datetime
    from email.utils import parsedate_to_datetime
    api_count = max(display * 3, 10)  # 48시간 필터링으로 많이 걸러질 수 있어 넉넉히 가져옴
    r = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        },
        params={"query": query, "display": api_count, "sort": "date"},
        timeout=12,
    )
    r.raise_for_status()
    tag_re = re.compile(r"<.*?>")
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for it in r.json().get("items", []):
        link = it.get("originallink") or it.get("link")
        if not link:
            continue
        try:
            pub_dt = parsedate_to_datetime(it.get("pubDate", ""))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
            age_hours = (now - pub_dt).total_seconds() / 3600
            if age_hours > max_age_hours:
                continue
        except Exception:
            continue  # 게시일을 확인할 수 없으면 안전하게 제외
        out.append({
            "title": tag_re.sub("", it.get("title", "")),
            "summary": tag_re.sub("", it.get("description", "")),
            "link": link,
        })
        if len(out) >= display:
            break
    return out


def fetch_foreign_news(query):
    """Claude의 web search로 해외 증권/경제 관련 실제 외신 기사를 찾아 한국어로 요약 (검색당 $0.01 + 토큰비용)"""
    prompt = f""""{query}" 관련 지금으로부터 48시간 이내에 게시된 해외 증권/경제 뉴스(블룸버그, CNBC, 로이터, WSJ 등)를 웹검색으로 찾아서,
실제로 검색 결과에서 확인한 기사 최대 3개를 아래 JSON으로만 답해, 다른 설명은 붙이지 마.

{{"articles": [{{"title": "기사 제목(한국어로 번역)", "summary": "핵심 팩트 2~3문장, 수치는 원문 그대로 유지", "link": "실제 기사 URL"}}]}}

반드시 지켜야 할 것:
- 게시일이 48시간을 넘었거나, 게시일을 검색 결과에서 확인할 수 없는 기사는 절대 포함하지 마
- 검색으로 확인되지 않은 내용은 절대 지어내지 마
- 조건에 맞는 기사가 없으면 {{"articles": []}}로 답해"""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    blocks = resp.json().get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    try:
        return extract_json(text).get("articles", [])
    except Exception:
        return []


def fact_check_topic(topic_question):
    """사용자가 고른 주제 1개에 대해서만 실제 뉴스기사로 사실관계를 확인 (게시된 지 48시간 이내인 기사만)
    (국내: 네이버 무료 검색 / 해외: Claude 웹서치 유료소액 검색, 둘 다 시도)"""
    articles = []
    try:
        for a in fetch_naver_news(topic_question):
            articles.append({**a, "source_type": "국내"})
    except Exception:
        pass
    try:
        for a in fetch_foreign_news(topic_question):
            if a.get("link"):
                articles.append({**a, "source_type": "해외"})
    except Exception:
        pass
    return articles


def call_claude(prompt, max_tokens=1500):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    blocks = resp.json().get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.strip()


def extract_json(text):
    """앞뒤에 설명이 붙어오거나 코드블록으로 감싸져도 안전하게 JSON 부분만 추출"""
    text = re.sub(r"^```json\s*|^```\s*|\s*```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"AI 응답에서 JSON을 찾지 못했어요: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def call_claude_json(prompt, max_tokens=1500, retries=2):
    """call_claude + JSON 파싱을 하나로 묶고, 실패하면 자동 재시도"""
    last_err = None
    for attempt in range(retries):
        try:
            text = call_claude(prompt, max_tokens=max_tokens)
            return extract_json(text)
        except Exception as e:
            last_err = e
            continue
    raise Exception(f"AI 응답을 이해하지 못했어요 (재시도 {retries}번 실패): {last_err}")


def ai_generate_topics(raw_data):
    prompt = f"""다음은 최근 금융/투자 관련 유튜브 인기 영상 제목(조회수 포함)과 구글 트렌드 급상승 연관검색어야.

{raw_data if raw_data.strip() else "(데이터 없음, 최근 시장 상황을 참고해서 일반적으로 관심 높은 주제로 대신 뽑아줘)"}

이 데이터를 참고해서, 시청자들이 실제로 궁금해할만한 콘텐츠 주제를 8개 뽑아줘.
각 주제는 짧고 흥미로운 질문 형태로.
**중요: 근거가 된 영상의 조회수가 높거나, 여러 데이터에서 반복적으로 나타나는 주제일수록 인기도가 높은 것으로 보고, 1번에 가장 인기도 높은 주제, 8번에 가장 낮은 주제가 오도록 인기도 순으로 정렬해줘.**
아래 JSON 형식으로만 답해, 다른 설명 붙이지 마:
{{"topics": ["주제/질문 1", "주제/질문 2", "..."]}}
"""
    return call_claude_json(prompt, max_tokens=1200)["topics"]


def ai_generate_package(topic_question, source_text, verified=True):
    if verified:
        fact_note = "아래는 실제로 확인된 뉴스 기사야. 여기 있는 사실관계와 수치만 사용하고, 없는 내용은 절대 지어내지 마."
        checklist_note = (
            "checklist는 반드시 근거자료의 사실관계에서만 도출해. 각 항목 뒤에 ' — ' 다음에 "
            "근거자료의 어떤 사실과 연결되는지 1줄로 덧붙여줘."
        )
    else:
        fact_note = ("이 주제를 뒷받침하는 실제 기사를 찾지 못했어. 구체적인 수치·통계·날짜를 새로 지어내지 말고, "
                     "'~하는 분위기다', '~라는 우려가 나온다' 같은 방향성 위주의 조심스러운 표현만 사용해.")
        checklist_note = (
            "실제 기사로 확인되지 않은 주제이므로, checklist는 구체적 수치 없이 "
            "'이런 상황에서 일반적으로 점검하면 좋은 것들' 위주의 일반론적 프레임워크로만 작성해."
        )

    prompt = f"""주제: {topic_question}

[근거자료 안내]
{fact_note}

[근거자료]
{source_text}

이 근거자료를 바탕으로 콘텐츠 패키지를 만들어줘. 아래 JSON 형식으로만 답해, 다른 설명 붙이지 마.
이모지는 각 지정된 필드에만 넣고, 그 외 텍스트(제목/본문/대본)에는 절대 넣지 마.

릴스 대본 2가지:
- reels_script_a: 30초 분량, 실제 말하는 대사 그대로, 후킹 문장으로 시작해서 본론-마무리 구조로
- reels_script_b: 30초 분량, 버전A와 다른 각도/톤으로 (예: 하나는 정보전달형, 하나는 스토리텔링형)

카드뉴스는 "5슬라이드 공식"을 반드시 따라서 고정 5장으로 만들어줘 (표지1 + 본문4):
- cover(슬라이드1/후킹): 두 줄 모두 최대 7어절 이내, 초등학생도 이해할 쉬운 단어만 사용 (전문용어·복문 절대 금지). 완전한 문장 아니어도 됨, 임팩트 있는 구(句) 형태 허용. 숫자는 단순하게 포함 (예: "-17%"). 1줄=사실/숫자, 2줄=반전 또는 질문으로 궁금증 유발. 위기감 도는 배경톤
  예) 1줄 "하루 만에 -6%" / 2줄 "근데 다들 웃었다?"
- slides[0](슬라이드2/팩트): 핵심 수치 **볼드** 포함한 팩트 제시
- slides[1](슬라이드3/반전): 모순되는 두 정보 대비 + 불안 증폭 마무리
- slides[2](슬라이드4/예고): "무엇을 점검해야 할까요?" 톤, 체크리스트 있다는 예고만 하고 내용은 절대 공개 금지
- slides[3](슬라이드5/CTA): 정확히 "팔로우 후 댓글에 **이모티콘**을 남겨주시면" 패턴으로 유도. 실제 이모지 문자를 넣지 말고 "이모티콘"이라는 단어 자체를 **볼드**로 감싸서 써 (렌더링 시 자동으로 네온그린 강조됨). DM이나 자료 전송 방식은 카드 문구에 절대 언급하지 마
문장은 짧고 단정적으로, 각 슬라이드는 다음 장이 궁금해지는 여운으로 마무리.

[슬라이드4에서 예고한 체크리스트를 실제로 작성해줘 — 발송용 자료]
- 카드에는 절대 내용을 공개하지 않지만, 이 체크리스트는 실제로 발송할 자료라서 진짜로 채워야 해
- {checklist_note}
- 5~7개 항목, 각 항목은 title(점검 포인트 제목, 짧게) / why(왜 중요한지, 근거자료의 어떤 사실과 연결되는지) / how(구체적으로 무엇을 어떻게 확인하면 되는지)로 구성
- why와 how는 따로 라벨을 붙이지 않고 이어붙여도 하나의 자연스러운 문단처럼 읽히게 써줘 (예: "~라는 신호예요. 그러니 ~를 확인해보세요" 처럼 근거→행동 순으로 매끄럽게 연결)
- 특정 종목 매수/매도 지시, 목표가, 수익률 예측 같은 직접적 투자 지시는 절대 쓰지 마 (정보 제공까지만)

{{
  "reels_script_a": "...",
  "reels_script_b": "...",
  "cover": {{"line1": "...", "line2": "...", "search_query": "위기감 도는 배경사진 영어검색어 2~4단어"}},
  "slides": [
    {{"emoji": "이모지1개", "title": "...", "body": "...", "search_query": "영어검색어"}},
    {{"emoji": "😰", "title": "...", "body": "...", "search_query": "영어검색어"}},
    {{"emoji": "🙌", "title": "...", "body": "...", "search_query": "영어검색어"}},
    {{"emoji": "💬", "title": "...", "body": "팔로우 후 댓글에 **이모티콘**을 남겨주시면 유도 문구 (DM/발송 언급 금지)", "search_query": "영어검색어"}}
  ],
  "caption": "질문 1줄 + 투표/댓글 유도, 짧게",
  "checklist": [
    {{"title": "점검 포인트 제목", "why": "왜 중요한지 (근거자료 사실 연결)", "how": "구체적으로 어떻게 확인하는지"}}
  ]
}}
"""
    return call_claude_json(prompt, max_tokens=3000)


def search_pexels_photo(query, used_ids=None, lock=None):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": 10, "orientation": "portrait"},
        timeout=12,
    )
    r.raise_for_status()
    photos = r.json().get("photos", [])
    if not photos:
        raise Exception(f"'{query}' 사진을 찾지 못했어요")

    def pick():
        chosen = None
        if used_ids is not None:
            for p in photos:
                if p["id"] not in used_ids:
                    chosen = p
                    break
        if chosen is None:
            chosen = photos[0]
        if used_ids is not None:
            used_ids.add(chosen["id"])
        return chosen

    if lock is not None:
        with lock:
            chosen = pick()
    else:
        chosen = pick()

    return requests.get(chosen["src"]["large2x"], timeout=20).content


def load_photo(photo_bytes):
    photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    return ImageOps.fit(photo, (W, H), method=Image.LANCZOS)


# ============ 표지: 사진 + 하단 고정 2줄 헤드라인 ============
def make_cover(photo_bytes, line1, line2, page_label):
    line1, line2 = flatten_text(strip_emoji(line1)), flatten_text(strip_emoji(line2))
    img = load_photo(photo_bytes)
    apply_gradient(img, top_alpha=110, bottom_alpha=245)
    draw = ImageDraw.Draw(img, "RGBA")

    draw_watermark(draw)

    safe_width = W - SAFE_PAD * 2 - 40
    size = 80
    l1_lines = wrap_text(draw, line1, font(F_TITLE, size), safe_width)
    l2_lines = wrap_text(draw, line2, font(F_TITLE, size), safe_width)
    # line1, line2 합쳐서 화면에 항상 2줄까지만 나오도록 폰트 크기를 계속 축소
    while (len(l1_lines) + len(l2_lines)) > 2 and size > 34:
        size -= 4
        l1_lines = wrap_text(draw, line1, font(F_TITLE, size), safe_width)
        l2_lines = wrap_text(draw, line2, font(F_TITLE, size), safe_width)

    line_h = int(size * 1.3)
    total_h = (len(l1_lines) + len(l2_lines)) * line_h
    bar_top = H - 130 - total_h
    bar_bottom = H - 130

    draw.rectangle([SAFE_PAD, bar_top, SAFE_PAD + 6, bar_bottom], fill=WHITE)

    y = bar_top
    for line in l1_lines:
        draw.text((SAFE_PAD + 34, y), line, font=font(F_TITLE, size), fill=WHITE)
        y += line_h
    for line in l2_lines:
        draw.text((SAFE_PAD + 34, y), line, font=font(F_TITLE, size), fill=BRAND_GREEN)
        y += line_h

    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - 70), page_label, font=font(F_MONO, 22), fill=WHITE)

    return img.convert("RGB")


# ============ 본문 카드: 사진 + 이모지·제목(가운데) + 문단(가운데, 볼드강조) ============
def make_content_card(photo_bytes, emoji, title, body, page_label):
    img = load_photo(photo_bytes)
    apply_gradient(img, top_alpha=195, bottom_alpha=230)
    draw = ImageDraw.Draw(img, "RGBA")

    draw_watermark(draw)

    safe_width = W - SAFE_PAD * 2

    title_text = flatten_text(strip_emoji(title))
    body = flatten_text(strip_emoji(body))
    title_size = 56
    body_size = 38
    title_lines, body_lines = [], []
    for attempt in range(4):
        emoji_size = int(title_size * 0.85)
        gap = int(title_size * 0.3)
        narrowed_width = safe_width - emoji_size - gap  # 이모지+간격만큼 좁힌 너비로 통일해서 줄바꿈
        title_lines = wrap_text(draw, title_text, font(F_TITLE, title_size), narrowed_width)
        body_lines = wrap_mixed_tokens(draw, tokenize_bold(body), body_size, safe_width)
        total_h = len(title_lines) * int(title_size * 1.3) + 60 + len(body_lines) * int(body_size * 1.7)
        if total_h <= (H - 340) or attempt == 3:
            break
        title_size -= 4
        body_size -= 2

    title_h = len(title_lines) * int(title_size * 1.3)
    body_h = len(body_lines) * int(body_size * 1.7)
    total_h = title_h + 60 + body_h
    y = (H - total_h) / 2

    emoji_img = fetch_emoji_image(emoji, size=emoji_size)

    for idx, line in enumerate(title_lines):
        text_w = draw.textlength(line, font=font(F_TITLE, title_size))
        if idx == 0 and emoji_img:
            gap = int(title_size * 0.3)
            total_w = emoji_size + gap + text_w
            x = (W - total_w) / 2
            emoji_y = int(y + (title_size * 1.3 - emoji_size) / 2)
            img.paste(emoji_img, (int(x), emoji_y), emoji_img)
            draw.text((x + emoji_size + gap, y), line, font=font(F_TITLE, title_size), fill=WHITE)
        else:
            draw.text(((W - text_w) / 2, y), line, font=font(F_TITLE, title_size), fill=WHITE)
        y += int(title_size * 1.3)

    y += 60
    for line in body_lines:
        draw_centered_mixed_line(draw, y, line, body_size, WHITE, BRAND_GREEN)
        y += int(body_size * 1.7)

    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - 70), page_label, font=font(F_MONO, 22), fill=WHITE)

    return img.convert("RGB")


def send_photo_group(chat_id, images):
    media = []
    files = {}
    for i, img in enumerate(images):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        key = f"photo{i}"
        files[key] = (f"{key}.png", buf, "image/png")
        media.append({"type": "photo", "media": f"attach://{key}"})
    requests.post(
        f"{TELEGRAM_API}/sendMediaGroup",
        data={"chat_id": chat_id, "media": json.dumps(media)},
        files=files,
        timeout=30,
    )


def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": chat_id, "text": text})


def send_checklist_message(chat_id, checklist):
    """슬라이드5(CTA)에서 예고한 체크리스트를 실제로 전송.
    title/why/how 구조는 유지하되, 라벨 없이 하나의 자연스러운 문단으로 이어붙여서 가독성을 높임.
    안내문구는 AI 출력에 의존하지 않고 코드에 고정해서 항상 붙게 함."""
    if not checklist:
        return
    blocks = []
    for i, item in enumerate(checklist, 1):
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            why = str(item.get("why", "")).strip()
            how = str(item.get("how", "")).strip()
            body = " ".join(p for p in [why, how] if p)
            blocks.append(f"{i}. 📍 {title}\n{body}" if body else f"{i}. 📍 {title}")
        else:
            blocks.append(f"{i}. 📍 {item}")
    disclaimer = "\n\n※ 이 자료는 정보 제공 목적이며 투자 권유가 아니에요. 투자 판단과 그에 따른 책임은 본인에게 있습니다."
    send_message(chat_id, "📌 발송용 체크리스트\n\n" + "\n\n".join(blocks) + disclaimer)


def send_buttons(chat_id, text, buttons):
    """buttons: [(라벨, callback_data), ...] 한 줄에 버튼 하나씩"""
    keyboard = {"inline_keyboard": [[{"text": label, "callback_data": data}] for label, data in buttons]}
    requests.post(f"{TELEGRAM_API}/sendMessage", data={
        "chat_id": chat_id, "text": text, "reply_markup": json.dumps(keyboard),
    })


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data=payload)


def download_telegram_file(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=20)
    file_path = r.json()["result"]["file_path"]
    return requests.get(f"{TELEGRAM_FILE_API}/{file_path}", timeout=20).content


def process_generation(chat_id, text):
    job = {"cancelled": False}
    ACTIVE_JOBS[chat_id] = job
    try:
        send_message(chat_id, "카드뉴스 만드는 중이에요, 잠시만 기다려주세요... (중단하려면 '취소'라고 보내주세요)")

        s = ai_generate_5slides(text)
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        cover = s["cover"]
        cover_query = cover.get("search_query", "business finance crisis")
        slides = s["slides"][:4]  # 5슬라이드 공식: 표지1 + 본문4 = 고정 5장
        manual_photo = PENDING_PHOTOS.pop(chat_id, None)
        used_photo_ids = set()

        cover_photo = manual_photo if manual_photo else search_pexels_photo(cover_query, used_photo_ids, PHOTO_LOCK)
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        total = 1 + len(slides)

        def fetch_one(c):
            q = c.get("search_query", cover_query)
            try:
                return search_pexels_photo(q, used_photo_ids, PHOTO_LOCK)
            except Exception:
                return search_pexels_photo(cover_query, used_photo_ids, PHOTO_LOCK)

        # 카드별 사진을 순서대로가 아니라 한꺼번에(동시에) 가져와서 속도를 크게 단축
        with ThreadPoolExecutor(max_workers=6) as pool:
            photo_results = list(pool.map(fetch_one, slides))

        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        images = [make_cover(cover_photo, cover["line1"], cover["line2"], f"1 / {total}")]
        for i, (c, photo_bytes) in enumerate(zip(slides, photo_results), 2):
            images.append(make_content_card(photo_bytes, c.get("emoji", ""), c["title"], c["body"], f"{i} / {total}"))

        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        send_photo_group(chat_id, images)
        if s.get("caption"):
            send_message(chat_id, "📋 캡션\n\n" + s["caption"])
        send_checklist_message(chat_id, s.get("checklist"))
    except Exception as e:
        send_message(chat_id, f"카드를 만드는 중 오류가 났어요: {e}")
    finally:
        ACTIVE_JOBS.pop(chat_id, None)


def process_recommend(chat_id):
    job = {"cancelled": False}
    ACTIVE_JOBS[chat_id] = job
    try:
        send_message(chat_id, "요즘 뜨는 금융/투자 주제를 훑어보는 중이에요, 잠시만요...")
        raw, references = collect_trend_data()
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return
        topics = ai_generate_topics(raw)
        PENDING_TOPICS[chat_id] = topics
        buttons = [(f"{i+1}. {t[:55]}{'...' if len(t) > 55 else ''}", f"topic:{i}") for i, t in enumerate(topics)]
        send_buttons(chat_id, (
            "요즘 관심 높은 주제들이에요 (인기도 높은 순). 버튼을 눌러 골라주세요 👇\n"
            "선택하면 실제 뉴스기사로 팩트체크까지 해드릴게요."
        ), buttons)

        if references:
            ref_lines = [f"{label}\n{url}" for label, url in references[:15]]
            send_message(chat_id, "🔥 참고한 인기 영상·트렌드 (인기도 참고용, 사실관계 근거 아님)\n\n" + "\n\n".join(ref_lines))
    except Exception as e:
        send_message(chat_id, f"주제를 찾는 중 오류가 났어요: {e}")
    finally:
        ACTIVE_JOBS.pop(chat_id, None)


def process_topic_select(chat_id, topic_question):
    """사용자가 버튼으로 주제를 고른 직후 실행: 그 주제 1개만 실제 뉴스기사로 팩트체크"""
    job = {"cancelled": False}
    ACTIVE_JOBS[chat_id] = job
    try:
        send_message(chat_id, f"'{topic_question}' 실제 뉴스로 팩트체크하는 중이에요...")
        articles = fact_check_topic(topic_question)
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        if articles:
            source_text = "\n\n".join(f"[{a['source_type']}] {a['title']}\n{a['summary']}" for a in articles)
            SELECTED_TOPIC[chat_id] = {"question": topic_question, "source_text": source_text, "verified": True}
            ref_lines = [
                f"{'📰' if a['source_type'] == '국내' else '🌐'} [{a['source_type']}] {a['title']}\n{a['link']}"
                for a in articles
            ]
            send_message(chat_id, "✅ 실제 기사로 확인됐어요\n\n🔎 팩트체크 근거기사\n\n" + "\n\n".join(ref_lines))
        else:
            SELECTED_TOPIC[chat_id] = {"question": topic_question, "source_text": topic_question, "verified": False}
            send_message(chat_id, "⚠️ 이 주제를 뒷받침하는 실제 기사를 찾지 못했어요. 구체적인 수치 없이 조심스러운 톤으로만 만들어드릴게요.")

        send_buttons(chat_id, f"'{topic_question}'\n\n뭘 만들어드릴까요?", [
            ("📝 릴스 대본만", "type:reels"),
            ("🖼️ 카드뉴스만", "type:cards"),
            ("🎬 둘 다", "type:both"),
        ])
    except Exception as e:
        send_message(chat_id, f"팩트체크 중 오류가 났어요: {e}")
    finally:
        ACTIVE_JOBS.pop(chat_id, None)


def process_package(chat_id, topic, kind="both"):
    """topic: {"question": str, "source_text": str, "verified": bool}"""
    job = {"cancelled": False}
    ACTIVE_JOBS[chat_id] = job
    try:
        label = {"reels": "릴스 대본", "cards": "카드뉴스", "both": "대본+카드뉴스"}.get(kind, "콘텐츠")
        send_message(chat_id, f"'{topic['question']}' 주제로 {label} 만드는 중이에요...")
        pkg = ai_generate_package(topic["question"], topic["source_text"], topic.get("verified", True))
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        if kind in ("reels", "both"):
            send_message(chat_id, "📝 릴스 대본 (버전 A)\n\n" + pkg["reels_script_a"])
            send_message(chat_id, "📝 릴스 대본 (버전 B)\n\n" + pkg["reels_script_b"])

        if kind not in ("cards", "both"):
            return

        cover = pkg["cover"]
        cover_query = cover.get("search_query", "business finance crisis")
        slides = pkg["slides"][:4]  # 5슬라이드 공식: 표지1 + 본문4 = 고정 5장
        used_photo_ids = set()
        cover_photo = search_pexels_photo(cover_query, used_photo_ids, PHOTO_LOCK)
        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        total = 1 + len(slides)

        def fetch_one(c):
            q = c.get("search_query", cover_query)
            try:
                return search_pexels_photo(q, used_photo_ids, PHOTO_LOCK)
            except Exception:
                return search_pexels_photo(cover_query, used_photo_ids, PHOTO_LOCK)

        with ThreadPoolExecutor(max_workers=6) as pool:
            photo_results = list(pool.map(fetch_one, slides))

        if job["cancelled"]:
            send_message(chat_id, "생성을 중단했어요.")
            return

        images = [make_cover(cover_photo, cover["line1"], cover["line2"], f"1 / {total}")]
        for i, (c, photo_bytes) in enumerate(zip(slides, photo_results), 2):
            images.append(make_content_card(photo_bytes, c.get("emoji", ""), c["title"], c["body"], f"{i} / {total}"))

        send_photo_group(chat_id, images)
        if pkg.get("caption"):
            send_message(chat_id, "📋 캡션\n\n" + pkg["caption"])
        send_checklist_message(chat_id, pkg.get("checklist"))
    except Exception as e:
        send_message(chat_id, f"패키지를 만드는 중 오류가 났어요: {e}")
    finally:
        ACTIVE_JOBS.pop(chat_id, None)


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    # 텔레그램이 응답 지연으로 같은 update를 재전송하는 경우, 한 번만 처리
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in PROCESSED_UPDATE_IDS:
            return "ok"
        PROCESSED_UPDATE_IDS.append(update_id)

    # ===== 버튼 클릭(callback_query) 처리 =====
    callback = update.get("callback_query")
    if callback:
        cb_chat_id = callback.get("message", {}).get("chat", {}).get("id")
        cb_data = callback.get("data", "")
        cb_id = callback.get("id")
        answer_callback(cb_id)

        if cb_chat_id is None:
            return "ok"

        if cb_data.startswith("topic:"):
            idx = int(cb_data.split(":", 1)[1])
            topics = PENDING_TOPICS.get(cb_chat_id, [])
            if 0 <= idx < len(topics):
                if cb_chat_id in ACTIVE_JOBS:
                    send_message(cb_chat_id, "이미 처리 중이에요! 잠시만 기다려주세요.")
                    return "ok"
                # 팩트체크는 웹서치가 껴서 시간이 걸릴 수 있어 백그라운드 스레드로 처리
                threading.Thread(target=process_topic_select, args=(cb_chat_id, topics[idx]), daemon=True).start()
            else:
                send_message(cb_chat_id, "주제 목록이 만료됐어요. '/추천'을 다시 입력해주세요.")
            return "ok"

        if cb_data.startswith("type:"):
            kind = cb_data.split(":", 1)[1]
            topic = SELECTED_TOPIC.pop(cb_chat_id, None)
            if not topic:
                send_message(cb_chat_id, "주제가 만료됐어요. '/추천'을 다시 입력해주세요.")
                return "ok"
            if cb_chat_id in ACTIVE_JOBS:
                send_message(cb_chat_id, "이미 만드는 중이에요! 중단하려면 '취소'라고 보내주세요.")
                return "ok"
            threading.Thread(target=process_package, args=(cb_chat_id, topic, kind), daemon=True).start()
            return "ok"

        return "ok"

    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return "ok"

    text = message.get("text", "") or message.get("caption", "")
    photos = message.get("photo")

    if text.strip() in ("취소", "/취소", "cancel", "/cancel"):
        job = ACTIVE_JOBS.get(chat_id)
        if job:
            job["cancelled"] = True
        else:
            send_message(chat_id, "지금 진행 중인 작업이 없어요.")
        return "ok"

    if text.startswith("/start"):
        send_message(chat_id, (
            "뉴스 원문을 그대로 붙여넣어 보내주세요.\n"
            "표지는 하단 2줄 헤드라인, 본문은 이모지+제목+문단 형식으로\n"
            "카드마다 어울리는 사진과 함께 만들어드릴게요.\n\n"
            "표지 사진을 직접 고르고 싶으면, 사진 먼저 보내고 원문을 이어서 보내주세요.\n"
            "만드는 중에 멈추고 싶으면 '취소'라고 보내주세요.\n\n"
            "키워드 없이 요즘 뜨는 주제를 추천받고 싶으면 '/추천'이라고 보내주세요.\n"
            "주제를 고르면 실제 뉴스기사로 팩트체크까지 해드려요."
        ))
        return "ok"

    if photos:
        file_id = photos[-1]["file_id"]
        try:
            PENDING_PHOTOS[chat_id] = download_telegram_file(file_id)
            send_message(chat_id, "표지 사진 받았어요! 이제 원문을 이어서 보내주세요.")
        except Exception as e:
            send_message(chat_id, f"사진을 받는 중 오류가 났어요: {e}")
        return "ok"

    if not text.strip():
        return "ok"

    if chat_id in ACTIVE_JOBS:
        send_message(chat_id, "이미 만드는 중이에요! 중단하려면 '취소'라고 보내주세요.")
        return "ok"

    if text.strip() in ("/추천", "추천"):
        threading.Thread(target=process_recommend, args=(chat_id,), daemon=True).start()
        return "ok"

    # 무거운 작업은 백그라운드 스레드로 넘기고, 텔레그램에는 즉시 응답 → 재전송(중복처리) 자체를 방지
    threading.Thread(target=process_generation, args=(chat_id, text), daemon=True).start()
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "bot is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
