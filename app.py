import os
import io
import re
import json
import requests
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageOps

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

HANDLE = "@로투파"

# 4:5 세로형 (인스타그램 캐러셀 권장 비율)
W, H = 1080, 1350
SAFE_PAD = 90
FOOTER_H = 100

# 스타일1 (본문 카드): 흰 배경 + 검은 제목 + 초록 불릿
BG_WHITE = (255, 255, 255)
TITLE_BLACK = (20, 22, 26)
BULLET_GREEN = (46, 196, 106)
BODY_BLACK = (35, 38, 44)
FOOTER_GRAY = (150, 154, 162)

# 스타일2 (표지): 사진 + 오버레이
ACCENT_BLUE = (66, 133, 255)
WHITE = (245, 246, 248)
BRAND_GREEN = (57, 255, 20)  # 로고와 동일한 네온 그린 (표지 제목 색상)

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# 챗별로 "표지용 사진을 기다리는 중" 상태를 잠깐 기억해두는 메모리 저장소
# (서버가 재시작되면 초기화되니, 사진 보낸 뒤 너무 오래 기다리면 다시 보내야 해요)
PENDING_PHOTOS = {}


def font(path, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, path), size)


F_TITLE = "IBMPlexSansKR-Bold.ttf"
F_BODY = "IBMPlexSansKR-Regular.ttf"
F_MONO = "IBMPlexMono-Medium.ttf"

NUM_RE = re.compile(r"[0-9][0-9,\.]*%?")


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


def draw_mixed_line(draw, xy, text, fnt, base_color, accent_color):
    """숫자/퍼센트 구간만 강조색으로 칠해서 한 줄 출력, 다음 줄 시작 y 좌표는 호출부에서 관리"""
    x, y = xy
    pos = 0
    for m in NUM_RE.finditer(text):
        if m.start() > pos:
            seg = text[pos:m.start()]
            draw.text((x, y), seg, font=fnt, fill=base_color)
            x += draw.textlength(seg, font=fnt)
        seg = m.group()
        draw.text((x, y), seg, font=fnt, fill=accent_color)
        x += draw.textlength(seg, font=fnt)
        pos = m.end()
    if pos < len(text):
        seg = text[pos:]
        draw.text((x, y), seg, font=fnt, fill=base_color)


# ============ 스타일 2: 표지 (사진 + 텍스트 오버레이) ============
def make_cover(photo_bytes, title, page_label):
    photo = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    photo = ImageOps.fit(photo, (W, H), method=Image.LANCZOS)
    img = photo.copy()
    draw = ImageDraw.Draw(img, "RGBA")

    # 하단부를 어둡게 그라데이션 처리해서 글자가 잘 보이게
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
        draw_mixed_line(draw, (SAFE_PAD, y), line, font(F_TITLE, title_size), BRAND_GREEN, BRAND_GREEN)
        y += int(title_size * 1.3)

    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - FOOTER_H + 30), page_label, font=font(F_MONO, 22), fill=WHITE)

    return img.convert("RGB")


# ============ 스타일 1: 본문 카드 (흰 배경 + 검은 제목 + 초록 불릿) ============
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
        for j, line in enumerate(wrapped):
            draw.text((SAFE_PAD + 34, y), line, font=font(F_BODY, bullet_size), fill=BODY_BLACK)
            y += int(bullet_size * 1.5)
        y += 24

    draw.line([(SAFE_PAD, H - FOOTER_H), (W - SAFE_PAD, H - FOOTER_H)], fill=(230, 230, 232), width=2)
    draw.text((SAFE_PAD, H - FOOTER_H + 26), HANDLE, font=font(F_MONO, 22), fill=FOOTER_GRAY)
    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((W - SAFE_PAD - w, H - FOOTER_H + 26), page_label, font=font(F_MONO, 22), fill=FOOTER_GRAY)

    return img


def parse_content_blocks(text):
    """빈 줄로 구분된 묶음 하나 = 카드 한 장. 첫 줄=제목, 나머지 줄=불릿 각각 한 줄씩."""
    blocks = [b.strip() for b in text.strip().split("\n\n") if b.strip()]
    cards = []
    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        title = lines[0]
        bullets = lines[1:]
        cards.append((title, bullets))
    return cards


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
            "카드뉴스를 만들어드릴게요.\n\n"
            "① 먼저 표지에 쓸 사진을 캡션과 함께 보내주세요.\n"
            "   캡션 예시: 반도체가 다시 흔들리기 시작했다\n\n"
            "② 그다음 나머지 카드 내용을 텍스트로 보내주세요.\n"
            "   빈 줄로 구분하면 그 묶음이 카드 한 장이 되고,\n"
            "   첫 줄은 제목, 나머지 줄은 불릿포인트가 돼요.\n\n"
            "예시:\n"
            "거래량 먼저 확인하세요\n"
            "평소 대비 3배 이상 터졌는지 확인\n"
            "장중 저점 대비 반등했는지도 체크\n\n"
            "지수 대비 낙폭도 비교하세요\n"
            "개별 종목이 SOXX보다 더 빠졌는지 확인"
        ))
        return "ok"

    try:
        if photos:
            # 가장 큰 해상도 사진 선택 → 표지용으로 임시 저장
            file_id = photos[-1]["file_id"]
            photo_bytes = download_telegram_file(file_id)
            title = text.strip() if text.strip() else "제목을 입력해주세요"
            PENDING_PHOTOS[chat_id] = {"photo": photo_bytes, "title": title}
            send_message(chat_id, "표지 사진 받았어요! 이제 나머지 카드 내용을 이어서 보내주세요.")
            return "ok"

        if not text.strip():
            return "ok"

        cards = parse_content_blocks(text)
        if not cards:
            send_message(chat_id, "내용을 인식하지 못했어요. /start 를 눌러서 형식을 확인해주세요.")
            return "ok"

        pending = PENDING_PHOTOS.pop(chat_id, None)
        total = len(cards) + (1 if pending else 0)
        images = []
        page = 1
        if pending:
            images.append(make_cover(pending["photo"], pending["title"], f"{page} / {total}"))
            page += 1
        for title, bullets in cards:
            images.append(make_content_card(title, bullets, f"{page} / {total}"))
            page += 1

        send_photo_group(chat_id, images)
    except Exception as e:
        send_message(chat_id, f"카드를 만드는 중 오류가 났어요: {e}")
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "bot is alive"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
