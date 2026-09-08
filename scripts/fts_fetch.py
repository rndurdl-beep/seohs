#!/usr/bin/env python3
"""영국 Find a Tender Service(FTS)에서 가속기·핵융합 관련 공고를 수집한다.

FTS의 검색 화면은 POST 폼이고 세션 상태를 서버에 두기 때문에 스크립트로는
다룰 수 없다(쿠키를 넘겨도 /syserror/fault 로 튕긴다). 대신 공식 OCDS API
(https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages)를 쓴다.

이 API에는 키워드 파라미터가 없다. updatedFrom/updatedTo 로 기간을 자르고
cursor 로 페이지를 넘기면서 받아온 뒤, 제목·설명을 클라이언트에서 필터링한다.

사용법:
    python3 scripts/fts_fetch.py                 # 최근 2일
    python3 scripts/fts_fetch.py --days 7
    python3 scripts/fts_fetch.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
PAGE_SIZE = 100
MAX_PAGES = 60          # 안전장치: 하루 공고량을 훨씬 넘는 값
TIMEOUT = 45
RETRIES = 4

# 키워드는 두 등급으로 나눈다. 영국 공고 전체를 훑어 문자열만 맞추면
# step / plasma(혈장) / accelerator(창업 액셀러레이터) 같은 오탐이 진짜 공고를
# 파묻어 버린다.

# 1등급: 이 말이 나오면 사실상 우리 분야가 확정인 것들.
STRONG = [
    "tokamak", "stellarator", "synchrotron", "cyclotron", "synchrocyclotron",
    "beamline", "beam line", "cryomodule", "klystron", "undulator",
    "waveguide", "rf cavity", "accelerating cavity", "superconducting cavity",
    "cavity resonator", "resonant cavity",
    "nuclear fusion", "fusion energy", "fusion reactor", "fusion power",
    "tritium", "divertor", "breeder blanket", "neutral beam",
    "particle accelerator", "linear accelerator", "linac",
    "ukaea", "uk fusion energy", "diamond light source", "isis neutron",
    "culham", "iter",
]

# 2등급: 단독으로는 애매해서 같은 문서에 CONTEXT 단어가 함께 있어야 인정한다.
# "rf"는 단어 경계로 찾으므로 RFID·RFP 같은 약어에는 걸리지 않지만, 그것만으로
# 우리 분야라 하기엔 약해서 2등급에 둔다.
WEAK = ["fusion", "accelerator", "plasma", "superconducting", "cryogenic",
        "rf", "radio frequency"]

CONTEXT = [
    "nuclear", "energy", "physics", "research facility", "reactor",
    "magnet", "vacuum", "cryostat", "neutron", "radiation", "isotope",
    "particle", "laboratory", "science", "beam", "megawatt", "kelvin",
]

# 2등급 단어가 이 표현으로 쓰였으면 그 자리는 아예 지운다.
FALSE_POSITIVES = [
    "data fusion", "fusion splicer", "fusion splicing", "image fusion",
    "spinal fusion", "fusion cuisine", "oracle fusion", "fusion welding",
    "sensor fusion", "fusion drink",
    "business accelerator", "startup accelerator", "start-up accelerator",
    "growth accelerator", "accelerator programme", "accelerator program",
    "accelerator cohort", "career accelerator", "concrete accelerator",
    "accelerator pedal", "digital accelerator", "innovation accelerator",
    "blood plasma", "plasma donation", "plasma freezer", "plasma screen",
    "plasma display", "plasma tv", "platelet", "plasma protein",
]


def run_curl(url: str) -> dict | None:
    """URL 하나를 받아 JSON으로 돌려준다. 실패 시 지수 백오프로 재시도."""
    last_err = ""
    for attempt in range(RETRIES):
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", str(TIMEOUT), url],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except ValueError as exc:
                last_err = f"JSON 파싱 실패: {exc}"
        else:
            last_err = (proc.stderr or proc.stdout or "").strip()[:200]
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
    print(f"  [경고] FTS 조회 실패: {last_err}", file=sys.stderr)
    return None


def _present(term: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def matches(text: str) -> list[str]:
    """본문에 걸린 관심 키워드 목록. 하나도 없으면 빈 리스트.

    1등급 키워드는 단독으로 인정하고, 2등급은 CONTEXT 단어가 함께 있을 때만
    인정한다. 오탐 표현은 검사 전에 본문에서 지운다.
    """
    low = text.lower()
    for fp in FALSE_POSITIVES:
        low = low.replace(fp, " ")

    hits = [kw for kw in STRONG if _present(kw, low)]

    weak_hits = [kw for kw in WEAK if _present(kw, low)]
    if weak_hits and any(_present(c, low) for c in CONTEXT):
        hits.extend(weak_hits)

    return hits


def collect(days: int) -> tuple[list[dict], int, bool]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    url = f"{API}?updatedFrom={since}&limit={PAGE_SIZE}"

    # 한 사업이 로트별로 여러 릴리스를 내므로 제목+발주기관으로 접는다.
    seen: set[tuple[str, str]] = set()
    matched: list[dict] = []
    scanned = 0
    complete = True
    for _ in range(MAX_PAGES):
        pkg = run_curl(url)
        if pkg is None:
            complete = False
            break
        releases = pkg.get("releases") or []
        scanned += len(releases)
        for rel in releases:
            tender = rel.get("tender") or {}
            haystack = " ".join([
                tender.get("title") or "",
                tender.get("description") or "",
                (rel.get("buyer") or {}).get("name") or "",
            ])
            hits = matches(haystack)
            if hits:
                dedupe_key = (tender.get("title") or "", (rel.get("buyer") or {}).get("name") or "")
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                period = tender.get("tenderPeriod") or {}
                value = tender.get("value") or {}
                matched.append({
                    "ocid": rel.get("ocid"),
                    "title": tender.get("title"),
                    "buyer": (rel.get("buyer") or {}).get("name"),
                    "status": tender.get("status"),
                    "published": rel.get("date"),
                    "deadline": period.get("endDate"),
                    "amount": value.get("amount"),
                    "currency": value.get("currency"),
                    "keywords": hits,
                    "url": f"https://www.find-tender.service.gov.uk/Notice/{(rel.get('ocid') or '').replace('ocds-h6vhtk-', '')}",
                })
        nxt = (pkg.get("links") or {}).get("next")
        if not nxt or not releases:
            break
        url = nxt
    else:
        complete = False  # MAX_PAGES 를 다 쓰고도 끝나지 않음

    matched.sort(key=lambda m: m.get("published") or "", reverse=True)
    return matched, scanned, complete


def to_markdown(rows: list[dict]) -> str:
    if not rows:
        return "신규 없음"
    out = ["| 공고명 | 발주기관 | 금액 | 마감일 | 상태 | 링크 |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        title = (r["title"] or "").replace("|", "/")
        amount = f"{r['amount']:,.0f} {r['currency']}" if r.get("amount") else "미공개"
        out.append(
            f"| {title} | {r.get('buyer') or '-'} | {amount} | "
            f"{(r.get('deadline') or '-')[:10]} | {r.get('status') or '-'} | "
            f"[공고문]({r['url']}) |"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Find a Tender 가속기·핵융합 공고 수집")
    parser.add_argument("--days", type=int, default=2, help="조회 기간(일), 기본 2")
    parser.add_argument("--json", action="store_true", help="마크다운 대신 원본 JSON 출력")
    args = parser.parse_args()

    rows, scanned, complete = collect(args.days)

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"## Find a Tender 신규 공고 (최근 {args.days}일, {scanned}건 중 {len(rows)}건 매칭)\n")
        print(to_markdown(rows))
    if not complete:
        print("\n[주의] 페이지를 끝까지 훑지 못했습니다. 결과가 불완전할 수 있습니다.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
