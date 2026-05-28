# -*- coding: utf-8 -*-
"""
나라장터 용역 입찰공고 모니터링 프로그램 (데일리 메일 + 대시보드 생성)
---------------------------------------------------------------
하는 일:
  1) 공공데이터포털 '나라장터 입찰공고정보서비스'에서 최근 용역 공고를 가져온다
  2) 관심 키워드(아트·전시·개발 등)에 일치하는 공고만 고른다
  3) 매일 아침 9시(한국시간), '오늘 새로 올라온' 관심 공고를 모아 한 통의 메일로 보낸다
     - 단, 관심 공고가 하나도 없으면 메일을 보내지 않는다
  4) 동시에 진행 중인 관심 공고 전체로 대시보드 HTML을 새로 만들어 둔다
     (GitHub Pages가 이 파일을 인터넷에 띄워 항상 접속 가능하게 함)

GitHub Actions가 매일 1회 자동 실행한다(UTC 0시 = 한국시간 오전 9시).
비밀값은 코드에 직접 쓰지 않고 GitHub Secrets에서 읽어온다.
"""

import os
import sys
import json
import html as htmllib
import smtplib
import datetime
from email.mime.text import MIMEText
from email.header import Header
import requests


# ============================================================
# 1. 설정 — 여기를 본인 상황에 맞게 바꾸면 된다
# ============================================================

# 관심 키워드: 공고명에 이 단어가 들어가면 알림 대상
KEYWORDS = ["아트", "전시", "개발", "미디어", "콘텐츠", "디자인", "sns", "마케팅"]
# 한 번에 가져올 공고 수 (최근 공고부터)
NUM_ROWS = 200

# 며칠 전까지의 공고를 대시보드에 표시할지 (마감 안 지난 진행 공고 위주)
LOOKBACK_DAYS = 14

# 대시보드 템플릿 / 출력 파일명
TEMPLATE_FILE = "dashboard_template.html"
OUTPUT_HTML = "index.html"     # GitHub Pages는 index.html을 기본 페이지로 띄운다


# ============================================================
# 2. 비밀값 — GitHub Secrets에서 읽어온다
# ============================================================

SERVICE_KEY = os.environ.get("SERVICE_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PW", "")
# 받는 사람: 쉼표로 여러 명 가능 (예: "a@x.com, b@y.com")
MAIL_TO_RAW = os.environ.get("MAIL_TO", "")
MAIL_TO = [addr.strip() for addr in MAIL_TO_RAW.split(",") if addr.strip()]

# 대시보드 공개 주소(메일 본문에 링크로 넣기 위함). 없으면 생략된다.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")


# ============================================================
# 3. 나라장터 API에서 용역 공고 가져오기
# ============================================================

def fetch_bids():
    """나라장터 입찰공고정보서비스 - 용역 공고 목록을 가져온다."""
    url = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"

    today = datetime.datetime.now()
    start = (today - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d0000")
    end = today.strftime("%Y%m%d2359")

    params = {
        "serviceKey": SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": str(NUM_ROWS),
        "inqryDiv": "1",
        "inqryBgnDt": start,
        "inqryEndDt": end,
        "type": "json",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    try:
        items = data["response"]["body"]["items"]
    except (KeyError, TypeError):
        print("응답에서 공고 목록을 찾지 못했습니다. 응답 일부:", str(data)[:300])
        return []
    if isinstance(items, dict):
        items = [items]
    return items


def parse_dt(s):
    """'2024-06-01 17:00' 같은 문자열을 날짜로. 실패하면 None."""
    if not s:
        return None
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:len(fmt)+2].strip(), fmt)
        except ValueError:
            continue
    return None


def extract(item):
    """API 공고 항목에서 필요한 필드만 뽑아 정리."""
    post = parse_dt(item.get("bidNtceDt", ""))      # 공고게시일시
    close = parse_dt(item.get("bidClseDt", ""))     # 입찰마감일시
    today = datetime.date.today()

    dday = None
    if close:
        dday = (close.date() - today).days

    amount = item.get("presmptPrce", "") or item.get("asignBdgtAmt", "") or "0"
    try:
        amount = int(float(str(amount).replace(",", "")))
    except (ValueError, TypeError):
        amount = 0

    return {
        "id": item.get("bidNtceNo", ""),
        "title": (item.get("bidNtceNm", "") or "").strip(),
        "org": (item.get("ntceInsttNm", "") or "").strip(),
        "postDate": post.date().isoformat() if post else today.isoformat(),
        "post_dt": post,
        "dday": dday if dday is not None else 999,
        "amount": amount,
        "url": item.get("bidNtceDtlUrl", "") or "https://www.g2b.go.kr",
    }


def matched_keywords(title):
    return [k for k in KEYWORDS if k in title]


# ============================================================
# 4. 대시보드 HTML 만들기 (템플릿에 데이터 주입)
# ============================================================

def build_dashboard(active_items):
    """진행 중인 관심 공고 목록으로 대시보드 HTML을 새로 만든다."""
    if not os.path.exists(TEMPLATE_FILE):
        print(f"템플릿 파일({TEMPLATE_FILE})이 없어 대시보드 생성을 건너뜁니다.")
        return

    rows = []
    for it in active_items:
        rows.append({
            "title": it["title"],
            "org": it["org"],
            "amount": it["amount"],
            "postDate": it["postDate"],
            "dday": it["dday"],
            "url": it["url"],
        })
    data_json = json.dumps(rows, ensure_ascii=False)
    kw_json = json.dumps([{"word": k} for k in KEYWORDS], ensure_ascii=False)

    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    synctime = now_kst.strftime("%m/%d %H:%M")

    tpl = open(TEMPLATE_FILE, encoding="utf-8").read()
    out = (tpl.replace("__DATA__", data_json)
              .replace("__KEYWORDS__", kw_json)
              .replace("__SYNCTIME__", synctime))
    open(OUTPUT_HTML, "w", encoding="utf-8").write(out)
    print(f"대시보드 생성 완료: {OUTPUT_HTML} (공고 {len(rows)}건)")


# ============================================================
# 5. 이메일 보내기 (Gmail) — 오늘 새 공고 다이제스트
# ============================================================

def send_email(new_items):
    today_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")
    parts = [f"<h2>오늘의 용역 입찰공고 {len(new_items)}건</h2>",
             f"<p>{today_str} · 관심 키워드 일치 공고입니다.</p>"]
    if DASHBOARD_URL:
        parts.append(f'<p><a href="{DASHBOARD_URL}">전체 대시보드 열기 →</a></p>')
    parts.append("<hr>")

    for it in new_items:
        kws = ", ".join(it["matched"])
        dd = "오늘 마감" if it["dday"] == 0 else f"D-{it['dday']}"
        title = htmllib.escape(it["title"])
        org = htmllib.escape(it["org"])
        parts.append(
            f'<p><b><a href="{it["url"]}">{title}</a></b><br>'
            f'{org} · 마감 {dd}<br>'
            f'<span style="color:#9a5b13">키워드: {htmllib.escape(kws)}</span></p>'
        )

    msg = MIMEText("\n".join(parts), "html", "utf-8")
    msg["Subject"] = Header(f"[나라장터] 오늘의 용역공고 {len(new_items)}건", "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(MAIL_TO)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PW)
        server.sendmail(GMAIL_USER, MAIL_TO, msg.as_string())
    print(f"메일 발송 완료: {len(new_items)}건 → {len(MAIL_TO)}명")


# ============================================================
# 6. 전체 흐름
# ============================================================

def main():
    missing = [n for n, v in [
        ("SERVICE_KEY", SERVICE_KEY), ("GMAIL_USER", GMAIL_USER),
        ("GMAIL_APP_PW", GMAIL_APP_PW), ("MAIL_TO", MAIL_TO_RAW)
    ] if not v]
    if missing:
        print("다음 설정값이 비어 있습니다:", ", ".join(missing))
        sys.exit(1)

    items = fetch_bids()
    print(f"가져온 공고: {len(items)}건")

    today = datetime.date.today()
    active_items = []   # 대시보드용: 진행 중(마감 안 지남)인 관심 공고 전체
    new_items = []      # 메일용: 오늘 게시된 관심 공고

    for raw in items:
        it = extract(raw)
        if not it["id"] or not it["title"]:
            continue
        kws = matched_keywords(it["title"])
        if not kws:
            continue
        it["matched"] = kws

        # 대시보드: 마감이 지나지 않은 공고만
        if it["dday"] >= 0:
            active_items.append(it)

        # 메일: 공고게시일이 오늘인 것만
        if it["post_dt"] and it["post_dt"].date() == today:
            new_items.append(it)

    # 정렬: 마감 임박 순
    active_items.sort(key=lambda x: x["dday"])
    new_items.sort(key=lambda x: x["dday"])

    print(f"진행 중 관심 공고(대시보드): {len(active_items)}건")
    print(f"오늘 새 관심 공고(메일): {len(new_items)}건")

    # 대시보드는 항상 최신으로 갱신
    build_dashboard(active_items)

    # 메일은 오늘 새 공고가 있을 때만
    if new_items:
        send_email(new_items)
    else:
        print("오늘 새 관심 공고가 없어 메일을 보내지 않습니다.")


if __name__ == "__main__":
    main()
