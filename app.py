import os
import io
import re
import json
import requests
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageOps

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

HANDLE = "@로투파"

W, H = 1080, 1350  # 4:5 세로형
SAFE_PAD = 90

WHITE = (245, 246, 248)
BRAND_GREEN = (57, 255, 20)  # 강조 색상

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

PENDING_PHOTOS = {}


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


def apply_gradient(img, top_alpha, bottom_alpha):
    """이미지 전체에 위→아래로 어두워지는 그라데이션(오버레이) 적용"""
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(H):
        alpha = int(top_alpha + (bottom_alpha - top_alpha) * (y / H))
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))


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


def draw_centered_mixed_line(draw, y, line, size, base_color):
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
        draw.text((x, y), word, font=fnt, fill=base_color)
        x += w + space_w
    return y


# ============ AI로 원문 → 카드 구조 변환 ============
def ai_structure_content(raw_text):
    prompt = f"""다음 뉴스 원문을 인스타그램 카드뉴스로 재구성해줘.
아래 JSON 형식으로만 답해. 다른 설명이나 문장은 절대 붙이지 마.

{{
  "thumbnail_line1": "표지 첫 줄 (짧게, 예: '워런 버핏이')",
  "thumbnail_line2": "표지 둘째 줄, 강조되는 핵심 문구 (예: '마지막으로 선택한 주식')",
  "search_query": "표지 배경 스톡사진 검색용 영어 키워드 2~4단어",
  "cards": [
    {{
      "emoji": "이 카드 내용을 대표하는 이모지 하나",
      "title": "카드 제목 (짧고 명확하게)",
      "body": "문단형 본문 2~4문장. 그중 가장 중요한 구절 하나는 **이렇게** 별표 두 개로 감싸서 강조 표시해줘.",
      "search_query": "이 카드 내용과 어울리는 배경 스톡사진 영어 검색어 2~4단어"
    }}
  ]
}}

- cards는 원문 분량에 따라 2~10장 사이로 알아서 나눠줘
- body는 불릿 없이 자연스럽게 이어지는 문단으로, 강조 구절은 딱 하나만 **로 감싸기
- 원문:
{raw_text}
"""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    return json.loads(text)


def search_pexels_photo(query):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": 5, "orientation": "portrait"},
        timeout=20,
    )
    r.raise_for_status()
    photos = r.json().get("photos", [])
    if not photos:
        raise Exception(f"'{query}' 사진을 찾지 못했어요")
    return requests.get(photos[0]["src"]["large2x"], timeout=20).content


def load_photo(photo_bytes):
    photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    return ImageOps.fit(photo, (W, H), method=Image.LANCZOS)


# ============ 표지: 사진 + 하단 고정 2줄 헤드라인 ============
def make_cover(photo_bytes, line1, line2, page_label):
    img = load_photo(photo_bytes)
    apply_gradient(img, top_alpha=40, bottom_alpha=190)
    draw = ImageDraw.Draw(img, "RGBA")

    draw.text((SAFE_PAD, 60), HANDLE, font=font(F_MONO, 24), fill=WHITE)

    safe_width = W - SAFE_PAD * 2 - 40
    size = 62
    l1_lines = wrap_text(draw, line1, font(F_TITLE, size), safe_width)
    l2_lines = wrap_text(draw, line2, font(F_TITLE, size), safe_width)
    while (len(l1_lines) + len(l2_lines)) > 4 and size > 40:
        size -= 6
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
    apply_gradient(img, top_alpha=120, bottom_alpha=170)
    draw = ImageDraw.Draw(img, "RGBA")

    draw.text((SAFE_PAD, 60), HANDLE, font=font(F_MONO, 24), fill=WHITE)

    safe_width = W - SAFE_PAD * 2

    title_text = f"{emoji} {title}".strip()
    title_size = 48
    body_size = 32
    title_lines, body_lines = [], []
    for attempt in range(4):
        title_lines = wrap_text(draw, title_text, font(F_TITLE, title_size), safe_width)
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

    for line in title_lines:
        w = draw.textlength(line, font=font(F_TITLE, title_size))
        draw.text(((W - w) / 2, y), line, font=font(F_TITLE, title_size), fill=WHITE)
        y += int(title_size * 1.3)

    y += 60
    for line in body_lines:
        draw_centered_mixed_line(draw, y, line, body_size, WHITE)
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


def download_telegram_file(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=20)
    file_path = r.json()["result"]["file_path"]
    return requests.get(f"{TELEGRAM_FILE_API}/{file_path}", timeout=20).content


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return "ok"

    text = message.get("text", "") or message.get("caption", "")
    photos = message.get("photo")

    if text.startswith("/start"):
        send_message(chat_id, (
            "뉴스 원문을 그대로 붙여넣어 보내주세요.\n"
            "표지는 하단 2줄 헤드라인, 본문은 이모지+제목+문단 형식으로\n"
            "카드마다 어울리는 사진과 함께 만들어드릴게요.\n\n"
            "표지 사진을 직접 고르고 싶으면, 사진 먼저 보내고 원문을 이어서 보내주세요."
        ))
        return "ok"

    try:
        if photos:
            file_id = photos[-1]["file_id"]
            PENDING_PHOTOS[chat_id] = download_telegram_file(file_id)
            send_message(chat_id, "표지 사진 받았어요! 이제 원문을 이어서 보내주세요.")
            return "ok"

        if not text.strip():
            return "ok"

        send_message(chat_id, "카드뉴스 만드는 중이에요, 잠시만 기다려주세요...")

        s = ai_structure_content(text)
        cover_query = s.get("search_query", "business finance")
        cards = s["cards"]

        manual_photo = PENDING_PHOTOS.pop(chat_id, None)
        cover_photo = manual_photo if manual_photo else search_pexels_photo(cover_query)

        total = 1 + len(cards)
        images = [make_cover(cover_photo, s["thumbnail_line1"], s["thumbnail_line2"], f"1 / {total}")]

        for i, c in enumerate(cards, 2):
            q = c.get("search_query", cover_query)
            try:
                photo_bytes = search_pexels_photo(q)
            except Exception:
                photo_bytes = search_pexels_photo(cover_query)
            images.append(make_content_card(photo_bytes, c.get("emoji", ""), c["title"], c["body"], f"{i} / {total}"))

        send_photo_group(chat_id, images)
    except Exception as e:
        send_message(chat_id, f"카드를 만드는 중 오류가 났어요: {e}")
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "bot is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
