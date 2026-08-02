import os
import io
import json
import requests
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== 색상 ======
BG = (12, 16, 22)          # 깔끔한 단색 배경
TITLE_GREEN = (30, 219, 96)  # 비비드 그린 (본제목)
INK = (232, 236, 241)       # 본문
INK_DIM = (139, 147, 163)   # 워터마크/페이지 번호

HANDLE = "@로투파"

SIZE = 1080  # 인스타그램 정사각형 카드뉴스 기준
SAFE_PAD = 100  # 안전 영역 여백 (이 안쪽에만 텍스트가 들어감, 잘림 방지)
FOOTER_H = 110  # 하단 워터마크 영역 높이

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")


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


def draw_footer(draw, page_label):
    y = SIZE - FOOTER_H
    draw.line([(SAFE_PAD, y), (SIZE - SAFE_PAD, y)], fill=(36, 43, 54), width=2)
    draw.text((SAFE_PAD, y + 22), HANDLE, font=font(F_MONO, 22), fill=INK_DIM)
    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((SIZE - SAFE_PAD - w, y + 22), page_label, font=font(F_MONO, 22), fill=INK_DIM)


def make_card(title, body, page_label):
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)

    safe_width = SIZE - SAFE_PAD * 2
    content_top = SAFE_PAD + 40
    content_bottom = SIZE - FOOTER_H - 40

    # 본문 줄 수에 따라 폰트 크기를 자동으로 낮춰서 안전 영역을 벗어나지 않게 함
    body_size = 34
    title_size = 52
    title_lines, body_lines = [], []
    for attempt in range(4):
        title_lines = wrap_text(draw, title, font(F_TITLE, title_size), safe_width)
        body_lines = wrap_text(draw, body, font(F_BODY, body_size), safe_width) if body else []
        total_h = len(title_lines) * int(title_size * 1.3) + 30 + len(body_lines) * int(body_size * 1.55)
        if total_h <= (content_bottom - content_top) or attempt == 3:
            break
        title_size -= 6
        body_size -= 3

    y = content_top
    for line in title_lines:
        draw.text((SAFE_PAD, y), line, font=font(F_TITLE, title_size), fill=TITLE_GREEN)
        y += int(title_size * 1.3)

    y += 30
    for line in body_lines:
        draw.text((SAFE_PAD, y), line, font=font(F_BODY, body_size), fill=INK)
        y += int(body_size * 1.55)

    draw_footer(draw, page_label)
    return img


def parse_input(text):
    """
    빈 줄로 구분된 묶음 하나 = 카드 한 장.
    묶음의 첫 줄 = 본제목, 나머지 줄 = 본문.
    """
    blocks = [b.strip() for b in text.strip().split("\n\n") if b.strip()]
    if not blocks:
        return []

    cards = []
    for block in blocks:
        lines = block.split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        body = " ".join(body.split("\n")).strip()
        cards.append((title, body))

    total = len(cards)
    images = []
    for i, (title, body) in enumerate(cards, 1):
        images.append(make_card(title, body, f"{i} / {total}"))
    return images


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


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    if not chat_id or not text:
        return "ok"

    if text.startswith("/start"):
        requests.post(f"{TELEGRAM_API}/sendMessage", data={
            "chat_id": chat_id,
            "text": (
                "카드뉴스를 만들어드릴게요.\n\n"
                "빈 줄(엔터 두 번)로 구분해서 보내주세요. 한 묶음이 카드 한 장이 돼요.\n\n"
                "예시:\n"
                "반도체가 다시 흔들리기 시작했다\n"
                "엔비디아 -3.5%, 마이크론 -9.9%\n\n"
                "거래량 먼저 확인하세요\n"
                "평소 대비 3배 이상 터졌는지가 핵심입니다"
            )
        })
        return "ok"

    try:
        images = parse_input(text)
        if not images:
            requests.post(f"{TELEGRAM_API}/sendMessage", data={
                "chat_id": chat_id, "text": "내용을 인식하지 못했어요. /start 를 눌러서 형식을 확인해주세요."
            })
            return "ok"
        send_photo_group(chat_id, images)
    except Exception as e:
        requests.post(f"{TELEGRAM_API}/sendMessage", data={
            "chat_id": chat_id,
            "text": f"카드를 만드는 중 오류가 났어요: {e}"
        })
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "bot is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
