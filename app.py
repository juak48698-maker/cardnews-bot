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
FOOTER_H = 100

BG_WHITE = (255, 255, 255)
TITLE_BLACK = (20, 22, 26)
BULLET_GREEN = (46, 196, 106)
BODY_BLACK = (35, 38, 44)
FOOTER_GRAY = (150, 154, 162)

WHITE = (245, 246, 248)
BRAND_GREEN = (57, 255, 20)  # 표지 제목 색상 (로고와 동일)

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# 챗별로 "표지용 사진을 기다리는 중" 상태를 잠깐 기억해두는 메모리 저장소
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


# ============ AI로 원문 → 카드 구조 변환 ============
def ai_structure_content(raw_text):
    prompt = f"""다음 뉴스 원문을 인스타그램 카드뉴스로 재구성해줘.
아래 JSON 형식으로만 답해. 다른 설명이나 문장은 절대 붙이지 마.

{{
  "thumbnail_title": "표지에 들어갈 임팩트있는 헤드라인 (18자 내외, 핵심 키워드/숫자 포함)",
  "search_query": "표지 배경 스톡사진 검색용 영어 키워드 2~4단어 (예: japan yen currency)",
  "cards": [
    {{"title": "카드 제목 (짧고 명확하게)", "bullets": ["핵심 포인트 한 문장", "핵심 포인트 한 문장"]}}
  ]
}}

- cards는 원문 분량에 따라 2~10장 사이로 알아서 나눠줘 (내용이 적으면 2장, 아주 많으면 10장까지)
- 각 카드의 bullets는 2~3개, 각각 간결한 한 문장으로
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
            "max_tokens": 2500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    # 혹시 ```json 코드블록으로 감싸서 오면 제거
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
        raise Exception("사진을 찾지 못했어요")
    img_url = photos[0]["src"]["large2x"]
    return requests.get(img_url, timeout=20).content


# ============ 표지 (사진 + 텍스트 오버레이) ============
def make_cover(photo_bytes, title, page_label):
    photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    photo = ImageOps.fit(photo, (W, H), method=Image.LANCZOS)
    img = photo.copy()
    draw = ImageDraw.Draw(img, "RGBA")

    grad_h = int(H * 0.55)
    for i in range(grad_h):
        alpha = int(210 * (i / grad_h))
        y = H - grad_h + i
        draw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))

    safe_width = W - SAFE_PAD * 2
    title_size = 68
    title_lines = wrap_text(draw, title, font(F_TITLE, title_size), safe_width)
    while len(title_lines) > 3 and title_size > 40:
        title_size -= 6
        title_lines = wrap_text(draw, title, font(F_TITLE, title_size), safe_width)

    handle_y = H - FOOTER_H - 30 - len(title_lines) * int(title_size * 1.3) - 50
    draw.text((SAFE_PAD, handle_y), HANDLE, font=font(F_MONO, 26), fill=WHITE)

    y = handle_y + 46
    for line in title_lines:
        draw.text((SAFE_PAD, y), line, font=font(F_TITLE, title_size), fill=BRAND_GREEN)
        y += int(title_size * 1.3)

    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - FOOTER_H + 30), page_label, font=font(F_MONO, 22), fill=WHITE)

    return img.convert("RGB")


# ============ 본문 카드 (흰 배경 + 검은 제목 + 초록 불릿) ============
def make_content_card(title, bullets, page_label):
    img = Image.new("RGB", (W, H), BG_WHITE)
    draw = ImageDraw.Draw(img)

    safe_width = W - SAFE_PAD * 2
    content_top = SAFE_PAD + 30
    content_bottom = H - FOOTER_H - 30

    title_size = 58
    bullet_size = 34
    title_lines, bullet_lines_per_item = [], []

    for attempt in range(4):
        title_lines = wrap_text(draw, title, font(F_TITLE, title_size), safe_width)
        bullet_lines_per_item = []
        for b in bullets:
            wrapped = wrap_text(draw, b, font(F_BODY, bullet_size), safe_width - 50)
            bullet_lines_per_item.append(wrapped)
        title_h = len(title_lines) * int(title_size * 1.25)
        bullets_h = sum(len(w) for w in bullet_lines_per_item) * int(bullet_size * 1.5) + len(bullets) * 24
        total_h = title_h + 50 + bullets_h
        if total_h <= (content_bottom - content_top) or attempt == 3:
            break
        title_size -= 6
        bullet_size -= 3

    y = content_top
    for line in title_lines:
        draw.text((SAFE_PAD, y), line, font=font(F_TITLE, title_size), fill=TITLE_BLACK)
        y += int(title_size * 1.25)

    y += 50
    for wrapped in bullet_lines_per_item:
        dot_y = y + bullet_size // 2 - 6
        draw.ellipse([SAFE_PAD, dot_y, SAFE_PAD + 12, dot_y + 12], fill=BULLET_GREEN)
        for line in wrapped:
            draw.text((SAFE_PAD + 34, y), line, font=font(F_BODY, bullet_size), fill=BODY_BLACK)
            y += int(bullet_size * 1.5)
        y += 24

    draw.line([(SAFE_PAD, H - FOOTER_H), (W - SAFE_PAD, H - FOOTER_H)], fill=(230, 230, 232), width=2)
    draw.text((SAFE_PAD, H - FOOTER_H + 26), HANDLE, font=font(F_MONO, 22), fill=FOOTER_GRAY)
    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - FOOTER_H + 26), page_label, font=font(F_MONO, 22), fill=FOOTER_GRAY)

    return img


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
    file_url = f"{TELEGRAM_FILE_API}/{file_path}"
    return requests.get(file_url, timeout=20).content


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
            "제가 알아서 표지 제목을 만들고, 어울리는 사진을 찾고,\n"
            "나머지 내용을 카드 여러 장으로 정리해드릴게요.\n\n"
            "직접 고른 사진을 표지로 쓰고 싶으면,\n"
            "그 사진을 먼저 보내주시고 그다음 원문을 보내주세요."
        ))
        return "ok"

    try:
        if photos:
            file_id = photos[-1]["file_id"]
            photo_bytes = download_telegram_file(file_id)
            PENDING_PHOTOS[chat_id] = photo_bytes
            send_message(chat_id, "사진 받았어요! 이제 원문을 이어서 보내주세요.")
            return "ok"

        if not text.strip():
            return "ok"

        send_message(chat_id, "카드뉴스 만드는 중이에요, 잠시만 기다려주세요...")

        structured = ai_structure_content(text)
        thumbnail_title = structured["thumbnail_title"]
        search_query = structured.get("search_query", "business finance")
        cards = structured["cards"]

        manual_photo = PENDING_PHOTOS.pop(chat_id, None)
        cover_photo = manual_photo if manual_photo else search_pexels_photo(search_query)

        total = 1 + len(cards)
        images = [make_cover(cover_photo, thumbnail_title, f"1 / {total}")]
        for i, c in enumerate(cards, 2):
            images.append(make_content_card(c["title"], c["bullets"], f"{i} / {total}"))

        send_photo_group(chat_id, images)
    except Exception as e:
        send_message(chat_id, f"카드를 만드는 중 오류가 났어요: {e}")
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "bot is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
