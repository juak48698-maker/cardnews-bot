import os
import re
import io
import requests
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== 색상 (투자비서와 동일한 톤) ======
BG = (12, 16, 22)
AMBER = (240, 169, 58)
INK = (232, 236, 241)
INK_DIM = (139, 147, 163)
UP = (62, 207, 142)
DOWN = (240, 85, 75)

SIZE = 1080  # 인스타그램 정사각형 기준 해상도
PAD = 90

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def font(path, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, path), size)


# 폰트 파일은 fonts/ 폴더에 직접 넣어주셔야 해요 (README 참고)
F_DISPLAY_BOLD = "SpaceGrotesk-Bold.ttf"
F_MONO = "IBMPlexMono-Medium.ttf"
F_KR_BODY = "IBMPlexSansKR-Regular.ttf"
F_KR_BOLD = "IBMPlexSansKR-Bold.ttf"


def wrap_text(draw, text, fnt, max_width):
    """긴 한글 문장을 카드 폭에 맞게 자동으로 줄바꿈"""
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


def draw_candles(draw):
    """배경에 은은하게 깔리는 캔들차트 패턴 (계정 시그니처 요소)"""
    import random
    random.seed(7)
    x = 40
    while x < SIZE - 40:
        top = random.randint(150, 500)
        bottom = top + random.randint(150, 450)
        color = UP if random.random() > 0.45 else DOWN
        faded = tuple(int(c * 0.16 + BG[i] * 0.84) for i, c in enumerate(color))
        draw.line([(x, top), (x, bottom)], fill=faded, width=2)
        body_top = top + random.randint(10, 40)
        body_bottom = bottom - random.randint(10, 40)
        draw.rectangle([x - 14, body_top, x + 14, body_bottom], fill=faded)
        x += random.randint(70, 100)


def base_card():
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    draw_candles(draw)
    return img, draw


def draw_footer(draw, handle, page_label):
    draw.line([(PAD, SIZE - 110), (SIZE - PAD, SIZE - 110)], fill=(36, 43, 54), width=2)
    draw.text((PAD, SIZE - 90), handle, font=font(F_MONO, 22), fill=INK_DIM)
    w = draw.textlength(page_label, font=font(F_MONO, 22))
    draw.text((SIZE - PAD - w, SIZE - 90), page_label, font=font(F_MONO, 22), fill=AMBER)


def make_cover(title, subtitle, handle, page_label):
    img, draw = base_card()
    draw.text((PAD, 110), "이번 주 시황", font=font(F_MONO, 26), fill=AMBER)
    lines = wrap_text(draw, title, font(F_KR_BOLD, 64), SIZE - PAD * 2)
    y = 210
    for line in lines:
        draw.text((PAD, y), line, font=font(F_KR_BOLD, 64), fill=INK)
        y += 78
    if subtitle:
        y += 20
        for line in wrap_text(draw, subtitle, font(F_KR_BODY, 32), SIZE - PAD * 2):
            draw.text((PAD, y), line, font=font(F_KR_BODY, 32), fill=INK_DIM)
            y += 44
    draw_footer(draw, handle, page_label)
    return img


def make_checklist(title, items, handle, page_label):
    img, draw = base_card()
    draw.text((PAD, 100), "체크포인트", font=font(F_MONO, 26), fill=AMBER)
    draw.text((PAD, 150), title, font=font(F_KR_BOLD, 46), fill=INK)
    y = 280
    for i, item in enumerate(items, 1):
        num = f"{i:02d}"
        draw.text((PAD, y), num, font=font(F_MONO, 30), fill=AMBER)
        lines = wrap_text(draw, item, font(F_KR_BODY, 32), SIZE - PAD * 2 - 90)
        yy = y
        for line in lines:
            draw.text((PAD + 90, yy), line, font=font(F_KR_BODY, 32), fill=INK)
            yy += 44
        y = yy + 40
    draw_footer(draw, handle, page_label)
    return img


def make_cta(handle, page_label):
    img, draw = base_card()
    draw.text((PAD, 400), "저장해두세요", font=font(F_MONO, 26), fill=AMBER)
    draw.text((PAD, 450), "다음 매매 전에", font=font(F_KR_BOLD, 52), fill=INK)
    draw.text((PAD, 520), "이 체크리스트부터", font=font(F_KR_BOLD, 52), fill=INK)
    draw.text((PAD, 620), "뇌동매매 아닌지, 분할매수 하는지", font=font(F_KR_BODY, 28), fill=INK_DIM)
    draw.text((PAD, 660), "확인하고 들어가세요.", font=font(F_KR_BODY, 28), fill=INK_DIM)
    draw_footer(draw, handle, page_label)
    return img


def parse_input(text, handle):
    """
    제목: ...
    부제: ...
    1. ...
    2. ...
    형식으로 온 메시지를 파싱해서 카드 3장을 만듭니다.
    """
    title = ""
    subtitle = ""
    items = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("제목:"):
            title = line.replace("제목:", "").strip()
        elif line.startswith("부제:"):
            subtitle = line.replace("부제:", "").strip()
        elif re.match(r"^\d+[.)]\s*", line):
            items.append(re.sub(r"^\d+[.)]\s*", "", line))
    if not title:
        # 형식을 안 지켰으면 첫 줄을 제목으로 대신 사용
        first_line = text.strip().split("\n")[0]
        title = first_line[:40]

    total = 2 + (1 if items else 0)
    images = []
    images.append(make_cover(title, subtitle, handle, f"1 / {total}"))
    page = 2
    if items:
        images.append(make_checklist(title, items, handle, f"{page} / {total}"))
        page += 1
    images.append(make_cta(handle, f"{total} / {total}"))
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
    import json
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
            "text": "카드뉴스를 만들어드릴게요.\n\n이렇게 보내주세요:\n\n제목: 반도체가 다시 흔들리기 시작했다\n부제: 엔비디아 -3.5% · 마이크론 -9.9%\n1. 거래량 확인\n2. 지수 대비 낙폭 비교\n3. 다음 실적일 체크"
        })
        return "ok"

    try:
        handle = "@YOUR_HANDLE"  # 본인 계정 핸들로 바꿔주세요
        images = parse_input(text, handle)
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
