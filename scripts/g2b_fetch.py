#!/usr/bin/env python3
"""나라장터(G2B) OpenAPI에서 가속기·핵융합 관련 입찰공고를 수집한다.

조달청_나라장터 입찰공고정보서비스 (data.go.kr 15129394)를 업무구분 4종
(물품/용역/공사/외자) × 키워드로 조회하고, 공고번호 기준으로 중복을 제거해
마크다운 표로 출력한다.

환경변수 G2B_SERVICE_KEY 에 data.go.kr 인코딩 서비스키가 있어야 한다.
키는 이미 퍼센트 인코딩된 문자열이므로 재인코딩하지 않고 그대로 붙인다.

사용법:
    python3 scripts/g2b_fetch.py                 # 최근 2일 신규 공고
    python3 scripts/g2b_fetch.py --days 7        # 최근 7일
    python3 scripts/g2b_fetch.py --json          # 원본 레코드를 JSON으로
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"

# 업무구분별 오퍼레이션. 표시명 -> 오퍼레이션명
OPERATIONS = {
    "물품": "getBidPblancListInfoThngPPSSrch",
    "용역": "getBidPblancListInfoServcPPSSrch",
    "공사": "getBidPblancListInfoCnstwkPPSSrch",
    "외자": "getBidPblancListInfoFrgcptPPSSrch",
}

# bidNtceNm 은 부분일치 검색이므로 상위 개념 키워드를 넓게 던진다.
KEYWORDS = [
    "가속기",
    "핵융합",
    "빔라인",
    "방사광",
    "중이온",
    "사이클로트론",
    "토카막",
    "초전도",
    "플라즈마",
    "KSTAR",
    "ITER",
]

TIMEOUT = 30
RETRIES = 5
WORKERS = 3


def fetch(op: str, keyword: str, begin: str, end: str, service_key: str) -> list[dict] | None:
    """한 오퍼레이션/키워드 조합을 조회한다.

    실패 시 지수 백오프로 재시도하고, 끝내 실패하면 None 을 돌려준다.
    (빈 리스트는 "조회 성공, 결과 없음"이라 구분해야 한다.)
    """
    url = f"{BASE}/{op}?serviceKey={service_key}"
    args = [
        "curl", "-sS", "-G", url,
        "--max-time", str(TIMEOUT),
        "--data-urlencode", "pageNo=1",
        "--data-urlencode", "numOfRows=100",
        "--data-urlencode", "inqryDiv=1",          # 1 = 공고게시일시 기준
        "--data-urlencode", f"inqryBgnDt={begin}",
        "--data-urlencode", f"inqryEndDt={end}",
        "--data-urlencode", f"bidNtceNm={keyword}",
        "--data-urlencode", "type=json",
    ]
    last_err = ""
    for attempt in range(RETRIES):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                body = json.loads(proc.stdout)["response"]["body"]
            except (ValueError, KeyError) as exc:
                last_err = f"응답 파싱 실패: {exc} / {proc.stdout[:200]}"
            else:
                items = body.get("items") or []
                # totalCount 가 0이면 items 가 빈 문자열로 오는 경우가 있다.
                return items if isinstance(items, list) else []
        else:
            last_err = (proc.stderr or proc.stdout or "").strip()[:200]
        if attempt < RETRIES - 1:
            # 지터를 섞어 재시도가 한꺼번에 몰리지 않게 한다.
            time.sleep(2 ** attempt + random.uniform(0, 1))
    print(f"  [경고] {op}/{keyword} 조회 실패: {last_err}", file=sys.stderr)
    return None


def collect(days: int, service_key: str) -> tuple[list[dict], list[str]]:
    now = datetime.now()
    begin = (now - timedelta(days=days)).strftime("%Y%m%d0000")
    end = now.strftime("%Y%m%d%H%M")

    combos = [(label, op, kw) for label, op in OPERATIONS.items() for kw in KEYWORDS]

    # 44개 조합을 순차로 돌면 몇 분씩 걸린다. 그렇다고 많이 붙이면 서버가
    # 연결을 끊어(Recv failure) 재시도를 다 쓰고도 조합이 통째로 빈다.
    # WORKERS=3 정도가 속도와 성공률의 접점이다.
    results: dict[tuple[str, str, str], list[dict] | None] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch, op, kw, begin, end, service_key): (label, op, kw)
            for label, op, kw in combos
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    by_no: dict[str, dict] = {}
    failures: list[str] = []
    for label, op, keyword in combos:  # 결과 병합은 항상 같은 순서로
        items = results.get((label, op, keyword))
        if items is None:
            failures.append(f"{label}/{keyword}")
            continue
        for item in items:
            # 같은 공고가 여러 키워드에 걸리므로 공고번호+차수로 중복 제거
            key = f"{item.get('bidNtceNo')}-{item.get('bidNtceOrd')}"
            if key not in by_no:
                item["_업무구분"] = label
                item["_매칭키워드"] = [keyword]
                by_no[key] = item
            elif keyword not in by_no[key]["_매칭키워드"]:
                by_no[key]["_매칭키워드"].append(keyword)

    rows = sorted(by_no.values(), key=lambda i: i.get("bidNtceDt", ""), reverse=True)
    return rows, failures


def to_markdown(rows: list[dict]) -> str:
    if not rows:
        return "신규 없음"
    out = ["| 공고명 | 발주기관 | 업무 | 추정가격 | 마감일시 | 링크 |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        name = (r.get("bidNtceNm") or "").replace("|", "/")
        inst = r.get("dminsttNm") or r.get("ntceInsttNm") or "-"
        amount = r.get("presmptPrce") or r.get("asignBdgtAmt") or ""
        amount = f"{int(amount):,}원" if str(amount).isdigit() else "미공개"
        close = (r.get("bidClseDt") or "-")[:16]
        url = r.get("bidNtceDtlUrl") or r.get("bidNtceUrl") or ""
        link = f"[공고문]({url})" if url else "-"
        out.append(f"| {name} | {inst} | {r['_업무구분']} | {amount} | {close} | {link} |")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="나라장터 가속기·핵융합 입찰공고 수집")
    parser.add_argument("--days", type=int, default=2, help="조회 기간(일), 기본 2")
    parser.add_argument("--json", action="store_true", help="마크다운 대신 원본 JSON 출력")
    args = parser.parse_args()

    service_key = os.environ.get("G2B_SERVICE_KEY")
    if not service_key:
        print("환경변수 G2B_SERVICE_KEY 가 없습니다.", file=sys.stderr)
        return 1

    rows, failures = collect(args.days, service_key)

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"## 나라장터 신규 공고 (최근 {args.days}일, {len(rows)}건)\n")
        print(to_markdown(rows))
    if failures:
        print(f"\n조회 실패: {', '.join(failures)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
