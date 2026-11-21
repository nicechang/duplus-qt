#!/usr/bin/env python3
"""
두플러스(du.plus) 생명의 삶 (QT) 로그인 및 크롤링 스크립트.

기능:
1. .env 파일(워크스페이스 .env 또는 ~/.env)에서 DUPLUS_ID, DUPLUS_PW를 읽어
   두란노/두플러스 3단계 SSO 로그인을 수행하고 회원 번호(userNo)와 인증 토큰을 획득합니다.
   (--no-login 옵션 또는 계정 미설정 시 공개 memberNo=7491로 조회 가능)
2. 오늘 날짜, 특정 날짜(--date YYYY-MM-DD), 또는 기간(--start ~ --end)의
   '생명의 삶' 본문(성경 역본별 말씀, 오늘의 찬송, 묵상 도우미, 오늘의 말씀 요약,
   본문 해설 및 묵상 질문, 오늘의 기도, 묵상 에세이, 한절 묵상, 오늘의 명언, 맥체인 성경 읽기)을
   크롤링합니다.
3. 날짜별 마크다운 파일(YYYY-MM-DD.md)로 저장합니다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import html
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.du.plus"
AZURE_API_BASE = "https://dplusprd-c2f4a6ffechtbnbf.z01.azurefd.net"
APIM_SUBSCRIPTION_KEY = "TXRsR01aV0pQVWJlYWNGSk8zMXBRUT09"
DEFAULT_MEMBER_NO = "7491"
DEFAULT_BRAND_NO = "35"  # 35: 생명의삶, 56: 생명의삶 PLUS, 57: LivingLife, 36: 새벽나라(sena)


def load_dotenv(env_paths: list[Path]) -> dict[str, str]:
    """외부 패키지 없이 .env 파일을 파싱합니다."""
    env_vars: dict[str, str] = {}
    for path in env_paths:
        if not path.is_file():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in env_vars:
                    env_vars[key] = val
        except OSError:
            continue
    return env_vars


def clean_html_to_markdown(raw_text: str) -> str:
    """API 응답 내 HTML 태그(<u>, <br>, <p> 등)를 깔끔한 마크다운으로 변환합니다."""
    if not raw_text:
        return ""
    text = raw_text
    # <br>, <br/> -> 줄바꿈
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # <u>소제목</u> -> **소제목**
    text = re.sub(r"<u>(.*?)</u>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
    # <b>강조</b>, <strong>강조</strong> -> **강조**
    text = re.sub(r"<(b|strong)>(.*?)</\1>", r"**\2**", text, flags=re.IGNORECASE | re.DOTALL)
    # <p> 태그 정리
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    # 기타 남은 HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 엔티티 디코딩 (&lt;, &gt;, &amp;, &nbsp; 등)
    text = html.unescape(text)
    # 연속된 빈 줄 정리 (3줄 이상 -> 2줄)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class DuplusClient:
    """두플러스 로그인 및 생명의 삶 API 클라이언트."""

    def __init__(self) -> None:
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
        self.user_no: str = DEFAULT_MEMBER_NO
        self.token: str | None = None
        self.logged_in: bool = False

    def _request_json(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict | list | str:
        req_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        }
        if headers:
            req_headers.update(headers)
        if self.token and "Authorization" not in req_headers:
            req_headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        with self.opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    def login(self, user_id: str, user_pw: str) -> bool:
        """
        두플러스 3단계 로그인을 수행합니다.
        1. POST /api/v1/cryptData1 (ID/PW 암호화)
        2. POST /api/GetUser (두란노 통합 계정 검증)
        3. POST Azure FrontDoor /api/Account/sign-in (두플러스 토큰/userNo 발급)
        """
        # Step 1: cryptData1
        crypt_payload = json.dumps({"userId": user_id, "userPw": user_pw}).encode("utf-8")
        crypt_res = self._request_json(
            f"{BASE_URL}/api/v1/cryptData1",
            method="POST",
            data=crypt_payload,
            headers={"Content-Type": "application/json;charset=utf-8"},
        )
        if not isinstance(crypt_res, dict) or "userId" not in crypt_res:
            raise RuntimeError(f"로그인 암호화 단계(cryptData1) 실패: {crypt_res}")

        # Step 2: GetUser
        form_body = urllib.parse.urlencode(
            {"userid": crypt_res["userId"], "pw": crypt_res["userPw"]}
        ).encode("utf-8")
        get_user_res = self._request_json(
            f"{BASE_URL}/api/GetUser",
            method="POST",
            data=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )

        parsed_user: dict = {}
        if isinstance(get_user_res, dict):
            parsed_user = get_user_res
        elif isinstance(get_user_res, str):
            cleaned = re.sub(r"[\r\n]+", "", get_user_res).strip()
            try:
                parsed_user = json.loads(cleaned)
            except json.JSONDecodeError:
                # JS 객체 형태일 경우 키-값 추출
                result_m = re.search(r'["\']?result["\']?\s*:\s*["\']([^"\']+)["\']', cleaned)
                idx_m = re.search(r'["\']?idx["\']?\s*:\s*["\']([^"\']+)["\']', cleaned)
                email_m = re.search(r'["\']?email["\']?\s*:\s*["\']([^"\']*)["\']', cleaned)
                parsed_user = {
                    "result": result_m.group(1) if result_m else "False",
                    "idx": idx_m.group(1) if idx_m else "",
                    "email": email_m.group(1) if email_m else "",
                }

        if str(parsed_user.get("result")).lower() == "false" or not parsed_user.get("idx"):
            raise RuntimeError(
                "두플러스 로그인 실패: 아이디 또는 비밀번호를 확인해 주세요."
            )

        duranno_idx = str(parsed_user["idx"])

        # Step 3: Account/sign-in
        signin_payload = json.dumps(
            {
                "SNSIdx": duranno_idx,
                "SNSType": "001",
                "DeviceID": "Web",
                "AppType": "001",
                "DeviceOS": "004",
            }
        ).encode("utf-8")
        signin_res = self._request_json(
            f"{AZURE_API_BASE}/api/Account/sign-in",
            method="POST",
            data=signin_payload,
            headers={
                "Content-Type": "application/json;charset=utf-8",
                "ver": "1.0",
                "Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY,
            },
        )
        if isinstance(signin_res, dict) and signin_res.get("userNo") and str(signin_res.get("userNo")) != "0":
            self.user_no = str(signin_res["userNo"])
            self.token = signin_res.get("token")
            self.logged_in = True
            return True

        raise RuntimeError(f"두플러스 세션 발급(Account/sign-in) 실패: {signin_res}")

    def fetch_today_meta(self, brand_no: str = DEFAULT_BRAND_NO) -> dict:
        """오늘의 QT 메타데이터(id, 날짜, 제목 등)를 조회합니다."""
        url = (
            f"{BASE_URL}/api/qt/today"
            f"?memberNo={urllib.parse.quote(self.user_no)}&UTC=9&brandNo={urllib.parse.quote(brand_no)}"
        )
        res = self._request_json(url)
        if not isinstance(res, dict) or "id" not in res:
            raise RuntimeError(f"오늘의 QT 정보를 가져오지 못했습니다: {res}")
        return res

    def fetch_today_range(self, brand_no: str = DEFAULT_BRAND_NO) -> list[dict]:
        """최근/향후 15일간의 날짜별 QT id 목록을 조회합니다."""
        url = (
            f"{BASE_URL}/api/qt/todayRange"
            f"?memberNo={urllib.parse.quote(self.user_no)}&UTC=9&brandNo={urllib.parse.quote(brand_no)}"
        )
        res = self._request_json(url)
        if isinstance(res, dict) and isinstance(res.get("today"), list):
            return res["today"]
        return []

    def resolve_qt_id_for_date(
        self, target_date: datetime.date, brand_no: str = DEFAULT_BRAND_NO
    ) -> int:
        """
        특정 날짜(YYYY-MM-DD)에 해당하는 QT id를 찾습니다.
        1) /api/qt/todayRange 목록에 해당 날짜가 있으면 그 id를 반환합니다.
        2) 범위를 벗어난 날짜는 오늘의 QT 날짜와 id를 기준으로 일수 차이(delta)를 계산해
           추적합니다 (생명의 삶 QT id는 하루에 1씩 증가하며, 만일 오차가 있으면 보정합니다).
        """
        target_str_dot = target_date.strftime("%Y.%m.%d")

        # 1. todayRange에서 검색
        range_items = self.fetch_today_range(brand_no=brand_no)
        for item in range_items:
            home_date = str(item.get("homeDate", ""))
            if home_date.startswith(target_str_dot) and item.get("id"):
                return int(item["id"])

        # 2. 오늘의 QT 기준 일수 차이로 계산
        today_meta = self.fetch_today_meta(brand_no=brand_no)
        today_id = int(today_meta["id"])
        home_date_raw = str(today_meta.get("homeDate", ""))
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", home_date_raw)
        if not m:
            raise RuntimeError(f"오늘의 QT 날짜 형식을 파싱할 수 없습니다: {home_date_raw}")
        base_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        delta_days = (target_date - base_date).days
        candidate_id = today_id + delta_days

        # 보정 확인 (최대 3회)
        for _ in range(3):
            detail = self.fetch_qt_detail(candidate_id)
            detail_date_str = str(detail.get("date", "")).strip()
            dm = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", detail_date_str)
            if not dm:
                break
            actual_date = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))).date()
            if actual_date == target_date:
                return candidate_id
            diff = (target_date - actual_date).days
            candidate_id += diff

        return candidate_id

    def fetch_qt_detail(self, qt_id: int | str) -> dict:
        """특정 QT id의 상세 본문 전체 데이터를 가져옵니다."""
        url = (
            f"{BASE_URL}/api/web-view/qt/qt-detail"
            f"?id={qt_id}&userNo={urllib.parse.quote(self.user_no)}&UTC=9"
        )
        res = self._request_json(url)
        if not isinstance(res, dict) or not res.get("id"):
            raise RuntimeError(f"QT 상세 데이터(id={qt_id})를 가져오지 못했습니다: {res}")
        return res


def format_qt_markdown(
    detail: dict,
    include_versions: list[int] | None = None,
) -> tuple[str, str]:
    """
    QT 상세 JSON을 마크다운 문서 문자열로 변환합니다.
    반환값: (파일명용 YYYY-MM-DD 문자열, 마크다운 본문)
    """
    if include_versions is None:
        include_versions = [5]  # 기본: 5(개역개정). 6(우리말성경), 7(NIV) 선택 가능

    raw_date = str(detail.get("date", "")).strip()  # 예: "2026.11.17"
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", raw_date)
    if m:
        iso_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    else:
        iso_date = datetime.now().strftime("%Y-%m-%d")

    day_str = str(detail.get("day", "")).strip()  # 예: "화요일"
    qt_title = clean_html_to_markdown(str(detail.get("qtTitle", ""))).replace("\n", " ")
    sub_title = clean_html_to_markdown(str(detail.get("subTitle", ""))).replace("\n", " ")
    brand_name = str(detail.get("contentBrandName") or detail.get("contentTitle") or "생명의삶").strip()
    qt_id = str(detail.get("id", ""))

    # 카테고리별 콘텐츠 맵핑
    categories: dict[str, dict] = {}
    for item in detail.get("content", []) or []:
        cat_no = str(item.get("dplusCategoryNo", ""))
        cat_name = str(item.get("dplusCategoryName", "")).strip()
        cat_body = clean_html_to_markdown(str(item.get("content", "")))
        categories[cat_no] = {
            "name": cat_name,
            "body": cat_body,
            "author": str(item.get("contentAuthorName", "")).strip(),
        }

    # 성경 본문 역본별 정리
    version_names = {5: "개역개정", 6: "우리말성경", 7: "NIV"}
    verses_by_version: dict[int, list[str]] = {v: [] for v in include_versions}

    for verse_group in detail.get("bibleList", []) or []:
        for v_item in verse_group.get("dailyQT_Bible", []) or []:
            v_no = int(v_item.get("bibleVersionNo", 0))
            if v_no in verses_by_version:
                clause = v_item.get("bibleClause", "")
                content = str(v_item.get("bibleContent", "")).strip()
                verses_by_version[v_no].append(f"**{clause}** {content}")

    lines: list[str] = []
    # YAML Frontmatter (Obsidian 친화적 포맷)
    lines.append("---")
    lines.append(f'date: "{iso_date}"')
    if day_str:
        lines.append(f'day: "{day_str}"')
    lines.append(f'title: "{qt_title.replace(chr(34), chr(39))}"')
    lines.append(f'scripture: "{sub_title.replace(chr(34), chr(39))}"')
    lines.append(f'source: "두플러스 {brand_name}"')
    lines.append(f'qt_id: "{qt_id}"')
    lines.append("tags:")
    lines.append("  - 생명의삶")
    lines.append("  - QT")
    lines.append("---")
    lines.append("")

    # 제목 헤더
    header_date = f"{iso_date} ({day_str})" if day_str else iso_date
    lines.append(f"# {qt_title}")
    lines.append("")
    lines.append(f"- **날짜**: {header_date}")
    lines.append(f"- **본문**: {sub_title}")
    if "422" in categories and categories["422"]["body"]:
        lines.append(f"- **맥체인 성경 읽기**: {categories['422']['body']}")
    lines.append("")

    # 오늘의 말씀 요약 (102)
    if "102" in categories and categories["102"]["body"]:
        lines.append("## 오늘의 말씀 요약")
        lines.append("")
        lines.append(f"> {categories['102']['body']}")
        lines.append("")

    # 오늘의 찬송 (99)
    if "99" in categories and categories["99"]["body"]:
        lines.append("## 오늘의 찬송")
        lines.append("")
        lines.append(categories["99"]["body"])
        lines.append("")

    # 성경 본문
    lines.append(f"## 성경 본문 ({sub_title})")
    lines.append("")
    for v_no in include_versions:
        v_list = verses_by_version.get(v_no, [])
        if not v_list:
            continue
        if len(include_versions) > 1:
            lines.append(f"### {version_names.get(v_no, f'역본 {v_no}')}")
            lines.append("")
        for v_line in v_list:
            lines.append(v_line)
        lines.append("")

    # 묵상 도우미 (101)
    if "101" in categories and categories["101"]["body"]:
        lines.append("## 묵상 도우미")
        lines.append("")
        lines.append(categories["101"]["body"])
        lines.append("")

    # 본문 해설 및 묵상 질문 (103)
    if "103" in categories and categories["103"]["body"]:
        lines.append("## 본문 해설")
        lines.append("")
        lines.append(categories["103"]["body"])
        lines.append("")

    # 한절 묵상 (106)
    if "106" in categories and categories["106"]["body"]:
        lines.append("## 한절 묵상")
        lines.append("")
        lines.append(categories["106"]["body"])
        lines.append("")

    # 묵상 에세이 (105)
    if "105" in categories and categories["105"]["body"]:
        lines.append("## 묵상 에세이")
        lines.append("")
        lines.append(categories["105"]["body"])
        lines.append("")

    # 오늘의 기도 (104)
    if "104" in categories and categories["104"]["body"]:
        lines.append("## 오늘의 기도")
        lines.append("")
        lines.append(categories["104"]["body"])
        lines.append("")

    # 오늘의 명언 (107)
    if "107" in categories and categories["107"]["body"]:
        lines.append("## 오늘의 명언")
        lines.append("")
        lines.append(f"> {categories['107']['body'].replace(chr(10), chr(10) + '> ')}")
        lines.append("")

    # 개인 묵상 노트 섹션
    lines.append("## 나의 묵상 및 적용")
    lines.append("")
    lines.append("- **내게 주시는 말씀**: ")
    lines.append("- **묵상 및 적용**: ")
    lines.append("- **기도**: ")
    lines.append("")

    return iso_date, "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="두플러스(du.plus) 생명의 삶 로그인 및 날짜별 마크다운 크롤러"
    )
    parser.add_argument(
        "--date",
        help="특정 날짜 지정 (YYYY-MM-DD). 미지정 시 오늘의 생명의 삶을 가져옵니다.",
    )
    parser.add_argument(
        "--start-date",
        help="기간 크롤링 시작일 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        help="기간 크롤링 종료일 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--id",
        type=int,
        dest="qt_id",
        help="두플러스 QT 고유 id로 직접 조회 (예: 9563)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="마크다운 파일을 저장할 폴더 경로 (기본값: 현재 폴더)",
    )
    parser.add_argument(
        "--versions",
        default="5",
        help="포함할 성경 역본 번호 (콤마 구분: 5=개역개정, 6=우리말성경, 7=NIV. 기본값: 5, 예: 5,6)",
    )
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="로그인 과정을 건너뛰고 공개 API로만 조회합니다.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 같은 날짜의 마크다운 파일이 있어도 덮어씁니다.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    workspace_env = Path.cwd() / ".env"
    home_env = Path.home() / ".env"
    dotenv_vars = load_dotenv([workspace_env, home_env])

    duplus_id = os.environ.get("DUPLUS_ID") or dotenv_vars.get("DUPLUS_ID")
    duplus_pw = os.environ.get("DUPLUS_PW") or dotenv_vars.get("DUPLUS_PW")

    client = DuplusClient()

    if not args.no_login and duplus_id and duplus_pw:
        try:
            client.login(duplus_id, duplus_pw)
            print(f"[INFO] 두플러스 로그인 성공 (userNo={client.user_no})")
        except Exception as exc:
            print(f"[WARN] 로그인 실패 ({exc}) -> 기본 조회 모드로 계속 진행합니다.", file=sys.stderr)
    elif not args.no_login:
        print("[INFO] DUPLUS_ID / DUPLUS_PW가 설정되지 않아 공개 조회 모드로 실행합니다.")

    include_versions = [
        int(x.strip()) for x in args.versions.split(",") if x.strip().isdigit()
    ] or [5]

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    target_qt_ids: list[int] = []

    if args.qt_id:
        target_qt_ids.append(args.qt_id)
    elif args.start_date and args.end_date:
        start_d = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if end_d < start_d:
            print("[ERROR] --end-date가 --start-date보다 앞설 수 없습니다.", file=sys.stderr)
            return 1
        first_id = client.resolve_qt_id_for_date(start_d)
        days_count = (end_d - start_d).days + 1
        target_qt_ids = [first_id + i for i in range(days_count)]
    elif args.date:
        target_d = datetime.strptime(args.date, "%Y-%m-%d").date()
        target_qt_ids.append(client.resolve_qt_id_for_date(target_d))
    else:
        today_meta = client.fetch_today_meta()
        target_qt_ids.append(int(today_meta["id"]))

    saved_files: list[Path] = []
    for qid in target_qt_ids:
        detail = client.fetch_qt_detail(qid)
        iso_date, md_content = format_qt_markdown(detail, include_versions=include_versions)
        file_path = out_dir / f"{iso_date}.md"
        if file_path.exists() and not args.overwrite:
            print(f"[SKIP] 이미 파일이 존재합니다: {file_path} (덮어쓰려면 --overwrite 사용)")
            continue
        file_path.write_text(md_content, encoding="utf-8")
        saved_files.append(file_path)
        print(f"[OK] 저장 완료: {file_path} ({detail.get('subTitle', '')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
