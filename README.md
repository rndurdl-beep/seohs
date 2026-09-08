# seohs — 가속기·핵융합 조달정보 수집

국내외 가속기(accelerator)·핵융합(fusion) 관련 입찰·조달 공고를 매일 수집해
리포트로 만들기 위한 스크립트 모음. 매일 04:00 KST에 도는 Routine이 이 스크립트를
실행한 결과와, 스크립트로 다룰 수 없는 소스의 웹 조회 결과를 합쳐 리포트를 낸다.

## 스크립트

| 스크립트 | 소스 | 방식 |
|---|---|---|
| `scripts/g2b_fetch.py` | 나라장터(조달청) | 입찰공고정보서비스 OpenAPI, 키워드 검색 |
| `scripts/fts_fetch.py` | 영국 Find a Tender | OCDS API + 클라이언트측 키워드 필터 |

```bash
python3 scripts/g2b_fetch.py --days 2      # 마크다운 표
python3 scripts/g2b_fetch.py --days 7 --json
python3 scripts/fts_fetch.py --days 2
```

### 나라장터 (`g2b_fetch.py`)

환경변수 `G2B_SERVICE_KEY`에 [공공데이터포털 나라장터 입찰공고정보서비스](https://www.data.go.kr/data/15129394/openapi.do)
서비스키가 있어야 한다.

- **키는 이미 퍼센트 인코딩된 문자열이다.** `--data-urlencode` 등으로 다시
  인코딩하면 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`가 난다. URL에 그대로 붙일 것.
- 업무구분 4종(물품/용역/공사/외자) × 키워드 16개를 조회하고 공고번호로 중복을
  제거한다. 재공고는 같은 공고번호에 차수(`bidNtceOrd`)만 올라가므로 차수가
  높은 쪽만 남긴다.
- `가속기`는 GPU·NPU 계열 연산 가속기도 물어온다. `AI가속기`·`연산가속기` 등은
  제외하되, 분야 용어(`핵융합`, `중이온` 등)가 함께 있으면 살린다. 의료용
  선형가속기는 대상에 포함한다.
- `플라즈마`와 `RF`는 단독으로는 분야를 뜻하지 않는다(ICP 광학방출분광기, SDR
  무선 송수신기 등). 이 키워드로만 걸린 공고는 `CONTEXT_TERMS`가 함께 있을 때만
  남긴다. 판단은 키워드를 전부 모은 뒤에 한다 — `플라즈마`로 먼저 걸린 공고가
  나중에 `핵융합`으로도 걸릴 수 있기 때문이다.
- `RF`·`ITER`·`KSTAR`처럼 라틴 문자 키워드는 부분일치가 `RFID`·`P-XRF`·
  `ITERATION`까지 물어오므로, 앞뒤에 알파벳이 붙지 않은 단독 토큰일 때만
  인정한다(`BOUNDARY_KEYWORDS`).
- `inqryDiv=1`은 공고게시일시 기준 조회를 뜻한다.
- API가 간헐적으로 연결을 끊으므로 조합마다 5회까지 지수 백오프 재시도하고,
  그래도 실패한 조합은 2차 패스에서 동시성 없이 하나씩 다시 던진다. 실패는
  대개 동시 요청 때문이라 혼자 천천히 가면 대부분 통과한다. 워커 수를 줄이는
  것만으로는 해결되지 않았다(줄인 뒤에도 64개 중 1~3개가 계속 실패했다).

### Find a Tender (`fts_fetch.py`)

- **검색 화면은 스크립트로 못 쓴다.** `/Search/Results` 폼은 `method="post"`이고
  검색 상태를 서버 세션(`SRSI_FT_AUTH` 쿠키)에 둔다. GET 쿼리스트링
  (`Keywords=`, `keywords=`, `q=`, `searchTerm=`)은 전부 무시되어 전체 30만 건이
  그대로 반환되고, 쿠키를 받아 POST해도 `/syserror/fault`로 302된다.
- 그래서 OCDS API를 쓴다. 키워드 파라미터가 없으므로 `updatedFrom`/`updatedTo`로
  기간을 자르고 `cursor`로 페이지를 넘긴 뒤 제목·설명·발주기관을 클라이언트에서
  필터링한다.
- `fusion`/`accelerator`는 오탐이 많아(data fusion, spinal fusion, startup
  accelerator 등) `FALSE_POSITIVES` 목록으로 걸러낸다. 오탐이 보이면 이 목록에
  추가할 것.

## 스크립트로 다룰 수 없는 소스

리포트 작성 시 웹 조회로 보완한다.

| 소스 | 상태 |
|---|---|
| 한국핵융합에너지연구원 | `https://www.kfe.re.kr/board.es?mid=a10302010000&bid=0010` |
| 포항가속기연구소 | `https://pal.postech.ac.kr/ko/bbs/use/TYhCqnQDVrpQyxUG/list.do` (`www.` 붙이면 DNS 실패) |
| ITER | `https://www.iter.org/industry/procurement/overview-tenders/open-tenders` (입찰 참여는 I-PROC 등록 필요) |
| F4E | `https://industryportal.f4e.europa.eu/f4e-calls?status=open` |
| TED (EU) | JS 렌더링이라 본문 추출 불가. F4E 공고는 TED에도 동시 게재되므로 F4E 포털로 커버 |
| SAM.gov (미국) | 검색 페이지가 셸만 반환, API는 별도 키 필요 |
| KAERI, STEP Fusion | 실행 환경의 egress 프록시가 차단 |
