#!/usr/bin/env python3
"""단독·다가구 매매 수집 → 일반건축물 표제부 후보 비교 → CSV/SQLite 저장.

Python 3.10 이상, 외부 패키지 없음. 실행: python single_multi_house.py
인증키는 getpass로 실행할 때만 입력한다. 자세한 설정/한계는 README.md 참조.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade"
BUILDING_URL = "https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"
LEGAL_URL = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
VWORLD_URL = "https://api.vworld.kr/req/address"
BASE = Path(__file__).resolve().parent
NOTICE = "주소는 건축물대장과 비교한 미확정 후보이며, 점수는 정답 확률이 아닙니다."


class AnalysisError(Exception):
    """인증키나 요청 URL을 포함하지 않는 사용자용 오류."""


def text(value):
    return "" if value is None else str(value).strip()


def pick(row, *names, default=""):
    for name in names:
        value = row.get(name)
        if value is not None and text(value).lower() not in {"", "nan", "none", "<na>"}:
            return value
    return default


def number(value):
    value = re.sub(r"[,\s]", "", text(value))
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def integer(value):
    value = number(value)
    return int(value) if value is not None and value.is_integer() else None


def positive(value):
    value = number(value)
    return value if value is not None and value > 0 else None


def year(value):
    match = re.match(r"^(\d{4})", text(value))
    value = int(match[1]) if match else None
    return value if value and 1800 <= value <= date.today().year else None


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def months(start, end):
    def parse(value):
        if not re.fullmatch(r"\d{6}", text(value)):
            raise AnalysisError("조회기간은 YYYYMM 여섯 자리로 입력하세요.")
        try:
            return date(int(value[:4]), int(value[4:]), 1)
        except ValueError:
            raise AnalysisError("조회기간의 연월이 올바르지 않습니다.") from None
    first, last = parse(start), parse(end)
    if first > last:
        raise AnalysisError("조회 시작월은 종료월보다 늦을 수 없습니다.")
    result = []
    while first <= last:
        result.append(first.strftime("%Y%m"))
        first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return result


def read_csv(path):
    for encoding in ("utf-8-sig", "cp949"):
        try:
            with Path(path).open(encoding=encoding, newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames:
                    raise AnalysisError("CSV에 열 이름이 없습니다.")
                return [{text(k): v for k, v in row.items()} for row in reader]
        except UnicodeDecodeError:
            continue
    raise AnalysisError("CSV 인코딩을 읽을 수 없습니다. UTF-8 또는 CP949로 저장하세요.")


def read_source(path, table):
    path = Path(path)
    if not path.is_file():
        raise AnalysisError(f"입력 파일이 없습니다: {path.name}")
    if path.suffix.lower() == ".csv":
        return read_csv(path)
    if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise AnalysisError("기존 자료는 CSV 또는 SQLite 파일을 지정하세요.")
    # 읽기 전용: 파일 누락 시 빈 DB를 만들거나 기존 DB를 수정하지 않는다.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            raise AnalysisError(f"SQLite 테이블이 없습니다: {table}")
        safe = '"' + table.replace('"', '""') + '"'
        return [dict(r) for r in con.execute("SELECT * FROM " + safe)]


def parse_page(payload):
    """실거래 XML/건축물대장 JSON 및 XML 오류 응답을 엄격하게 해석한다."""
    try:
        if payload.lstrip().startswith("{"):
            data = json.loads(payload)
            response = data.get("response", {})
            header, body = response.get("header", {}), response.get("body", {})
            code = text(header.get("resultCode"))
            if code not in {"0", "00", "000"}:
                raise AnalysisError(f"공공데이터 API 오류(코드 {code or '없음'}). 활용신청·키·할당량을 확인하세요.")
            obj = body.get("items") or {}
            rows = obj.get("item", []) if isinstance(obj, dict) else []
            rows = [rows] if isinstance(rows, dict) else rows
            total, page = integer(body.get("totalCount")), integer(body.get("pageNo"))
        else:
            root = ET.fromstring(payload)
            for node in root.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
            code = text(root.findtext(".//resultCode"))
            if code not in {"0", "00", "000"}:
                portal = text(root.findtext(".//returnReasonCode"))
                raise AnalysisError(f"공공데이터 API 오류(코드 {code or portal or '없음'}). 활용신청·키·할당량을 확인하세요.")
            rows = [{n.tag: text(n.text) for n in item} for item in root.findall(".//items/item")]
            total, page = integer(root.findtext(".//totalCount")), integer(root.findtext(".//pageNo"))
    except (json.JSONDecodeError, ET.ParseError, AttributeError, TypeError):
        raise AnalysisError("API 응답 구조가 올바르지 않습니다. HTML/점검 응답일 수 있습니다.") from None
    if total is None or total < 0 or not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise AnalysisError("API 응답에 유효한 totalCount 또는 items가 없습니다.")
    return rows, total, page


class ApiClient:
    def __init__(self, cache_dir, refresh=False, cache_hours=24, delay=0.2):
        self.cache_dir = Path(cache_dir)
        self.refresh = refresh
        self.cache_hours = cache_hours
        self.delay = delay
        self.last_call = 0.0
        self.audit = []

    def request(self, url, params, secret="", secret_param="serviceKey"):
        params = dict(params)
        if secret:
            # unquote_plus를 사용하면 Decoding 키의 '+'가 공백으로 바뀐다.
            params[secret_param] = urllib.parse.unquote(secret.strip())
        request_url = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            wait = self.delay - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            try:
                request = urllib.request.Request(request_url, headers={"User-Agent": "SingleMultiHouseAnalysis/1.0"})
                with urllib.request.urlopen(request, timeout=40) as response:
                    return response.read().decode("utf-8-sig")
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise AnalysisError(f"API HTTP {status}. 활용신청·네트워크 허용·할당량을 확인하세요.") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    # 예외 원문/URL은 인증키를 포함할 수 있어 출력하지 않는다.
                    raise AnalysisError("API 연결 실패: 네트워크·인증서·응답 지연을 확인하세요.") from None
            time.sleep(2 ** attempt)
        raise AnalysisError("API 요청 재시도 실패")

    def collect(self, url, params, secret):
        signature = {"version": 1, "url": url, "params": params}
        cache = self.cache_dir / (stable_hash(signature) + ".json")
        if cache.exists() and not self.refresh:
            try:
                obj = json.loads(cache.read_text(encoding="utf-8"))
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(obj["fetched_at"])).total_seconds()
                if (obj.get("signature") == signature and 0 <= age < self.cache_hours * 3600
                        and obj.get("complete") is True and len(obj["rows"]) == obj["total"]):
                    self.audit.append({"요청": signature, "수집시각": obj["fetched_at"], "캐시사용": True, "건수": obj["total"]})
                    return obj["rows"]
            except (ValueError, KeyError, TypeError):
                pass
        all_rows, expected, page, seen = [], None, 1, set()
        while True:
            payload = self.request(url, {**params, "pageNo": page, "numOfRows": 100}, secret)
            rows, total, response_page = parse_page(payload)
            if response_page is not None and response_page != page:
                raise AnalysisError("API가 요청한 페이지와 다른 페이지를 반환했습니다.")
            if expected is not None and expected != total:
                raise AnalysisError("수집 중 totalCount가 변했습니다. --refresh로 다시 실행하세요.")
            expected = total
            if not rows and len(all_rows) != total:
                raise AnalysisError("전체 건수보다 적게 수집됐습니다. 빈 중간 페이지를 확인하세요.")
            fingerprint = stable_hash(rows)
            if rows and fingerprint in seen:
                raise AnalysisError("같은 페이지가 반복됐습니다. API 페이지 응답을 확인하세요.")
            seen.add(fingerprint)
            all_rows.extend(rows)
            if len(all_rows) > total:
                raise AnalysisError("수집 건수가 totalCount를 초과했습니다.")
            if len(all_rows) == total:
                break
            page += 1
            if page > 100000:
                raise AnalysisError("API 페이지 상한을 초과했습니다.")
        obj = {"signature": signature, "fetched_at": now(), "complete": True, "total": expected, "rows": all_rows}
        save_json(cache, obj)
        self.audit.append({"요청": signature, "수집시각": obj["fetched_at"], "캐시사용": False, "건수": expected})
        return all_rows

    def lookup_legal(self, full_name, secret):
        """이름으로 법정동코드 조회. 검색 결과의 첫 행을 임의로 선택하지 않는다."""
        matches, page, total_seen = set(), 1, 0
        while True:
            payload = self.request(LEGAL_URL, {"type": "json", "locatadd_nm": full_name,
                                              "pageNo": page, "numOfRows": 100}, secret, "ServiceKey")
            try:
                data = json.loads(payload)
                blocks = data["StanReginCd"]
                header = next(b["head"] for b in blocks if "head" in b)
                result = next(h["RESULT"] for h in header if "RESULT" in h)
                total = int(next(h["totalCount"] for h in header if "totalCount" in h))
                rows = [r for b in blocks for r in b.get("row", [])]
                if text(result.get("resultCode")) not in {"INFO-0", "0", "00"}:
                    raise AnalysisError("법정동코드 API 오류. 코드 직접 입력 또는 해당 API 활용신청을 확인하세요.")
            except (ValueError, KeyError, TypeError, StopIteration):
                raise AnalysisError("법정동코드 자동조회 실패. 10자리 법정동코드를 설정하면 이 API가 필요 없습니다.") from None
            for row in rows:
                code = text(row.get("region_cd"))
                if text(row.get("locatadd_nm")) == full_name and re.fullmatch(r"\d{10}", code):
                    matches.add(code)
            total_seen += len(rows)
            if total_seen >= total:
                break
            if not rows or page >= 1000:
                raise AnalysisError("법정동코드 목록 수집이 불완전합니다. 코드를 직접 지정하세요.")
            page += 1
        if len(matches) != 1:
            raise AnalysisError(f"법정동 이름이 없거나 모호합니다: {full_name}. 공식 10자리 코드를 지정하세요.")
        return matches.pop()


def checked_code(value, length):
    value = text(value)
    if not re.fullmatch(r"\d{" + str(length) + r"}", value):
        raise AnalysisError(f"지역코드는 문자열 {length}자리 숫자로 입력하세요: {value}")
    return value


def resolve_scopes(config, client, key, config_dir):
    """공식 법정동코드로 조회 범위를 정하고 중복 지역을 합친다."""
    scopes = {}
    for area in config.get("areas", []):
        sido, sgg = text(area.get("sido")), text(area.get("sigungu"))
        if not sido or not sgg:
            raise AnalysisError("지역마다 sido(공식 시도명), sigungu(시군구명)를 입력하세요.")
        sgg_code = text(area.get("sigungu_code"))
        if sgg_code:
            checked_code(sgg_code, 5)
        entries = []
        for dong in area.get("legal_dongs", []):
            if not isinstance(dong, dict) or not text(dong.get("name")):
                raise AnalysisError("legal_dongs는 name, code를 가진 객체 목록이어야 합니다.")
            entries.append((dong["name"], dong.get("code")))
        if area.get("administrative_dongs"):
            raise AnalysisError("이 프로그램은 법정동 기준입니다. 행정동 대신 legal_dongs에 법정동을 지정하세요.")
        if not entries:
            raise AnalysisError("지역마다 legal_dongs를 한 개 이상 지정하세요.")
        for name, code in entries:
            name = text(name)
            if not name:
                raise AnalysisError("법정동 이름이 비어 있습니다.")
            code = text(code) or client.lookup_legal(f"{sido} {sgg} {name}", key)
            checked_code(code, 10)
            if code.endswith("00000"):
                raise AnalysisError("시군구 코드 대신 실제 읍·면·동·리의 10자리 법정동코드를 지정하세요.")
            if sgg_code and code[:5] != sgg_code:
                raise AnalysisError(f"시군구코드와 법정동코드가 다릅니다: {name}")
            if code not in scopes:
                scopes[code] = {"code": code, "sido": sido, "sigungu": sgg, "name": name}
            scope = scopes[code]
            if (scope["sido"], scope["sigungu"], scope["name"]) != (sido, sgg, name):
                raise AnalysisError("같은 법정동코드에 서로 다른 지역명이 지정됐습니다.")
    if not scopes:
        raise AnalysisError("areas에 조회 지역을 입력하세요.")
    return list(scopes.values())


def parcel(value):
    value = re.sub(r"\s+|번지", "", text(value))
    value = value.replace("＊", "*").replace("–", "-")
    match = re.fullmatch(r"(산)?(\d+|[\d*]+)(?:-(\d+|[\d*]+))?", value)
    if not match:
        return None
    mountain, main, sub = bool(match[1]), match[2], match[3]
    main = str(int(main)) if "*" not in main else main
    sub = str(int(sub)) if sub is not None and "*" not in sub else sub
    if sub == "0":
        sub = None
    return mountain, main, sub


def parcel_matches(masked, candidate, mode="digit_count"):
    pattern, actual = parcel(masked), parcel(candidate)
    if not pattern or not actual or "*" in candidate or pattern[0] != actual[0]:
        return False
    def part_match(p, v):
        if v is None:
            return False
        expression = "".join((r"\d" if mode == "digit_count" else r"\d*") if c == "*" else re.escape(c) for c in p)
        return re.fullmatch(expression, v) is not None
    if not part_match(pattern[1], actual[1]):
        return False
    if pattern[2] is not None:
        return part_match(pattern[2], actual[2])
    # 마스킹 본번만 공개되면 부번이 생략됐을 수 있다. 무마스킹은 부번까지 일치.
    return "*" in pattern[1] or actual[2] is None


def make_trade_rows(raw_rows, scopes, selected_months):
    by_code = {s["code"]: s for s in scopes}
    by_name = defaultdict(list)
    for scope in scopes:
        by_name[(scope["code"][:5], scope["name"])].append(scope)
    result, occurrences = [], Counter()
    for row in raw_rows:
        region = text(pick(row, "sggCd", "지역코드", "조회_LAWD_CD", "_sgg"))
        dong = text(pick(row, "umdNm", "법정동"))
        legal_code = text(pick(row, "법정동코드", "legal_code"))
        if legal_code:
            scope = by_code.get(legal_code)
            if scope and (region and region != legal_code[:5] or dong and dong != scope["name"]):
                raise AnalysisError("실거래가의 법정동코드와 지역명/시군구코드가 충돌합니다.")
        else:
            candidates = by_name.get((region, dong), [])
            if len(candidates) > 1:
                raise AnalysisError("동일 시군구에 같은 법정동명이 여러 개입니다. 실거래가에 10자리 법정동코드가 필요합니다.")
            scope = candidates[0] if candidates else None
        if not scope:
            if not region or not dong:
                raise AnalysisError("실거래가에 시군구코드/법정동이 없습니다. 다른 지역 자료와 혼합하지 마세요.")
            continue
        y = integer(pick(row, "dealYear", "계약년"))
        m = integer(pick(row, "dealMonth", "계약월"))
        d = integer(pick(row, "dealDay", "계약일_일"))
        if d is None and text(row.get("계약일")):
            try:
                parsed_date = date.fromisoformat(text(row["계약일"])[:10])
                y, m, d = parsed_date.year, parsed_date.month, parsed_date.day
            except ValueError:
                pass
        try:
            contract = date(y, m, d)
        except (ValueError, TypeError):
            raise AnalysisError("거래자료의 계약일이 올바르지 않습니다.") from None
        if contract.strftime("%Y%m") not in selected_months:
            continue
        cancelled = text(pick(row, "cdealType", "해제여부"))
        cancel_date = text(pick(row, "cdealDay", "해제사유발생일"))
        fingerprint = stable_hash(row)
        occurrences[fingerprint] += 1
        # 동일 속성의 서로 다른 거래를 임의 중복제거하지 않는다.
        trade = {
            "거래ID": f"{fingerprint[:24]}-{occurrences[fingerprint]}",
            "시도": scope["sido"], "시군구": scope["sigungu"], "지역코드": region,
            "법정동": dong, "법정동코드": scope["code"],
            "계약일": contract.isoformat(), "계약년": y, "계약월": m, "계약일_일": d,
            "지번_마스킹": text(pick(row, "jibun", "지번_마스킹", "지번")),
            "주택유형": text(pick(row, "houseType", "주택유형")),
            "연면적_㎡": positive(pick(row, "totalFloorAr", "연면적_㎡", "연면적")),
            "대지면적_㎡": positive(pick(row, "plottageAr", "대지면적_㎡", "대지면적")),
            "건축년도": year(pick(row, "buildYear", "건축년도")),
            "거래금액_만원": number(pick(row, "dealAmount", "거래금액_만원")),
            "거래유형": text(pick(row, "dealingGbn", "거래유형")),
            "매수자구분": text(pick(row, "buyerGbn", "매수자구분")),
            "매도자구분": text(pick(row, "slerGbn", "sellerGbn", "매도자구분")),
            "해제여부": cancelled, "해제사유발생일": cancel_date,
            "취소거래": bool(cancel_date or cancelled.upper() not in {"", "N", "0", "아니오"}),
            "수집원본조회월": text(pick(row, "_month", "조회_DEAL_YMD")),
        }
        result.append(add_derivatives(trade))
    return result


def add_derivatives(trade):
    money, floor, land = trade["거래금액_만원"], trade["연면적_㎡"], trade["대지면적_㎡"]
    trade["거래금액_원"] = round(money * 10000) if money is not None else None
    trade["연면적_평"] = round(floor / 3.305785, 3) if floor else None
    trade["대지면적_평"] = round(land / 3.305785, 3) if land else None
    trade["연면적㎡당_만원"] = round(money / floor, 3) if money is not None and floor else None
    trade["대지㎡당_만원"] = round(money / land, 3) if money is not None and land else None
    age = trade["계약년"] - trade["건축년도"] if trade["건축년도"] else None
    trade["계약당시_경과연수"] = age if age is not None and age >= 0 else None
    trade["원자료경고"] = "건축년도가 계약년보다 늦음" if age is not None and age < 0 else ""
    return trade


def make_buildings(raw_rows, scopes):
    allowed = {s["code"]: s for s in scopes}
    grouped = defaultdict(list)
    seen = set()
    for row in raw_rows:
        sgg = text(pick(row, "sigunguCd", "sigungu_cd", "시군구코드", "_sgg"))
        bjd = text(pick(row, "bjdongCd", "bjdong_cd", "법정동코드_뒤5자리", "_bjd"))
        code = text(pick(row, "lawd_cd", "법정동코드")) or sgg + bjd
        if len(code) != 10:
            raise AnalysisError("건축물대장에 10자리 법정동코드 또는 sigunguCd/bjdongCd가 필요합니다.")
        if code not in allowed:
            continue
        gb = text(pick(row, "regstrGbCd", "regstr_gb_cd"))
        gb_name = text(pick(row, "regstrGbCdNm", "regstr_gb_nm"))
        if gb == "2" or "집합" in gb_name:
            continue
        kind = text(pick(row, "regstrKindCd", "regstr_kind_cd"))
        if kind and kind != "2":
            # 1=총괄표제부, 2=일반건축물, 3~5=집합 표제부/전유부 등.
            # 총괄/개별 표제부가 함께 입력되어 면적을 두 번 합산하는 것을 막는다.
            continue
        use = text(pick(row, "mainPurpsCdNm", "main_purps_cd_nm", "건축물대장_주용도"))
        other = text(pick(row, "etcPurps", "etc_purps"))
        residential = any(word in use + other for word in ("단독", "다가구", "다중주택", "공관"))
        if not residential or any(word in use for word in ("아파트", "다세대", "연립", "공동주택")):
            continue
        jibun = text(pick(row, "jibun", "jibun_norm", "건축물대장_지번"))
        plat_gb = text(pick(row, "platGbCd", "plat_gb_cd"))
        if not jibun:
            main = integer(pick(row, "bun"))
            sub = integer(pick(row, "ji", default="0"))
            if not main or main < 0 or sub is None or sub < 0:
                continue
            if plat_gb not in {"0", "1"}:
                continue
            jibun = ("산" if plat_gb == "1" else "") + str(main) + (f"-{sub}" if sub else "")
        parsed = parcel(jibun)
        if not parsed or "*" in jibun:
            continue
        if plat_gb == "1" and not parsed[0]:
            # 첨부된 기존 DB의 jibun은 bun/ji만으로 생성되어 '산'이 빠질 수 있다.
            parsed = (True, parsed[1], parsed[2])
        elif plat_gb == "0" and parsed[0]:
            raise AnalysisError("건축물대장의 산지번과 대지구분코드가 충돌합니다.")
        elif plat_gb not in {"", "0", "1"}:
            continue
        jibun = ("산" if parsed[0] else "") + parsed[1] + ("-" + parsed[2] if parsed[2] else "")
        identifier = text(pick(row, "mgmBldrgstPk", "bld_key"))
        identity = (code, identifier or stable_hash(row))
        if identity in seen:
            continue
        seen.add(identity)
        scope = allowed[code]
        record = {
            "법정동코드": code, "지번": jibun, "관리번호": identifier,
            "주소": text(pick(row, "platPlc", "plat_plc")) or f'{scope["sido"]} {scope["sigungu"]} {scope["name"]} {jibun}',
            "도로명주소": text(pick(row, "newPlatPlc", "new_plat_plc")),
            "연면적": positive(pick(row, "totArea", "tot_area")),
            "대지면적": positive(pick(row, "platArea", "plat_area")),
            "건축년도": year(pick(row, "useAprDay", "use_apr_day", "approval_year")),
            "주용도": use, "기타용도": other,
            "대장구분확인": gb == "1" or "일반" in gb_name,
        }
        grouped[(code, jibun)].append(record)
    parcels = []
    for (code, jibun), rows in grouped.items():
        # 개별동 표제부만 합산한다. 총괄표제부/전유부는 수집하지 않는다.
        floor_values = [r["연면적"] for r in rows]
        lands = {r["대지면적"] for r in rows if r["대지면적"] is not None}
        warnings = []
        if len(rows) > 1:
            warnings.append("동일지번_복수동합산가설_부분거래와불일치가능")
        if len(lands) > 1:
            warnings.append("동별대지면적상이_대지점수미반영")
        if not all(r["대장구분확인"] for r in rows):
            warnings.append("일반대장구분코드누락")
        if any(v is None for v in floor_values):
            warnings.append("연면적누락_불완전합산금지")
        parcels.append({
            "법정동코드": code, "지번": jibun,
            "주소": rows[0]["주소"], "도로명주소": ";".join(sorted({r["도로명주소"] for r in rows if r["도로명주소"]})),
            "연면적": sum(floor_values) if all(v is not None for v in floor_values) else None,
            "대지면적": next(iter(lands)) if len(lands) == 1 and all(r["대지면적"] for r in rows) else None,
            "건축년도목록": sorted({r["건축년도"] for r in rows if r["건축년도"]}),
            "주용도": ";".join(sorted({r["주용도"] for r in rows})),
            "기타용도": ";".join(sorted({r["기타용도"] for r in rows})),
            "관리번호": ";".join(sorted(r["관리번호"] for r in rows if r["관리번호"])),
            "대장동수": len(rows), "대장경고": ";".join(warnings),
        })
    return parcels


def area_similarity(a, b, weight):
    if a is None or b is None or a <= 0 or b <= 0:
        return 0.0, None, None, False
    diff, ratio = abs(a - b), abs(a - b) / a
    # 0.1㎡ 이내 오차는 허용. 20% 오차에서 0점, 항목 가중치는 고정.
    similarity = 1.0 if diff <= 0.1 else max(0.0, 1 - ratio / 0.20)
    return round(similarity * weight, 4), round(diff, 4), round(ratio * 100, 4), True


def score_candidate(trade, building):
    floor_score, floor_diff, floor_pct, floor_ok = area_similarity(trade["연면적_㎡"], building["연면적"], 40)
    land_score, land_diff, land_pct, land_ok = area_similarity(trade["대지면적_㎡"], building["대지면적"], 30)
    years = building["건축년도목록"]
    year_diff = min((abs(trade["건축년도"] - y) for y in years), default=None) if trade["건축년도"] else None
    year_score = max(0, 20 - year_diff * 4) if year_diff is not None else 0
    typ = trade["주택유형"]
    use_text = building["주용도"] + building["기타용도"]
    if "다가구" in typ:
        type_score = 10 if "다가구" in use_text else 5 if "단독" in use_text else 0
    elif "단독" in typ:
        type_score = 10 if "단독" in use_text and "다가구" not in use_text else 5
    else:
        type_score = 0
    detail = {
        "유사도점수": round(floor_score + land_score + year_score + type_score, 2),
        "연면적점수": round(floor_score, 2), "대지면적점수": round(land_score, 2),
        "건축년도점수": year_score, "유형점수": type_score,
        "연면적차이_㎡": floor_diff, "연면적차이율_%": floor_pct,
        "대지면적차이_㎡": land_diff, "대지면적차이율_%": land_pct,
        "건축년도차이": year_diff,
        "비교가능가중치": (40 if floor_ok else 0) + (30 if land_ok else 0) + (20 if year_diff is not None else 0) + (10 if typ else 0),
    }
    return detail


def match_trades(trades, buildings, settings):
    by_code = defaultdict(list)
    for building in buildings:
        by_code[building["법정동코드"]].append(building)
    summaries, candidates = [], []
    for trade in trades:
        base = {**trade, "주소확정여부": "미확정", "주의사항": NOTICE}
        if trade["취소거래"] and not settings.get("include_cancelled", False):
            summaries.append({**base, "매칭상태": "취소거래_분석제외", "전체패턴후보수": 0, "기준통과후보수": 0})
            continue
        pattern_candidates = [b for b in by_code[trade["법정동코드"]]
                              if parcel_matches(trade["지번_마스킹"], b["지번"], settings.get("mask_mode", "digit_count"))]
        scored = []
        for building in pattern_candidates:
            detail = score_candidate(trade, building)
            max_year_gap = settings.get("max_year_gap", 10)
            if detail["건축년도차이"] is not None and detail["건축년도차이"] > max_year_gap:
                continue
            if detail["유사도점수"] < settings.get("min_score", 40):
                continue
            scored.append({**detail, "_building": building})
        scored.sort(key=lambda r: (-r["유사도점수"], r["_building"]["지번"]))
        common = {"전체패턴후보수": len(pattern_candidates), "기준통과후보수": len(scored)}
        if not scored:
            reason = ("지번패턴해석불가" if parcel(trade["지번_마스킹"]) is None else
                      "해당법정동_대장없음" if not by_code[trade["법정동코드"]] else
                      "지번패턴후보없음" if not pattern_candidates else "점수또는연도기준미달")
            summaries.append({**base, **common, "매칭상태": "미매칭", "미매칭사유": reason})
            continue
        best = scored[0]
        gap = round(best["유사도점수"] - scored[1]["유사도점수"], 2) if len(scored) > 1 else None
        top = best["_building"]
        ambiguous = len(scored) > 1 and gap < settings.get("ambiguity_gap", 5)
        has_digits = bool(re.search(r"\d", trade["지번_마스킹"]))
        if ambiguous:
            status = "복수후보_구분어려움"
        elif best["유사도점수"] >= 85 and best["비교가능가중치"] == 100 and has_digits and not top["대장경고"]:
            status = "상대적고유사_검증필요"
        else:
            status = "추정후보_검증필요"
        summaries.append({**base, **common, "매칭상태": status,
                          "1순위후보주소": top["주소"], "1순위유사도점수": best["유사도점수"],
                          "1_2순위점수차": gap, "1순위대장경고": top["대장경고"]})
        # top_n은 표시 개수만 제한한다. 동점/모호성은 전체 후보로 판단한다.
        for rank, row in enumerate(scored[:settings.get("top_n", 5)], 1):
            building = row["_building"]
            details = {k: v for k, v in row.items() if k != "_building"}
            candidates.append({**base, **common, **details, "매칭상태": status, "후보순위": rank,
                               "1_2순위점수차": gap, "후보주소": building["주소"],
                               "후보지번": building["지번"], "후보도로명주소": building["도로명주소"],
                               "대장연면적_㎡": building["연면적"], "대장대지면적_㎡": building["대지면적"],
                               "대장건축년도목록": ";".join(map(str, building["건축년도목록"])),
                               "대장주용도": building["주용도"], "대장동수": building["대장동수"],
                               "대장관리번호": building["관리번호"], "대장경고": building["대장경고"]})
    return summaries, candidates


def add_coordinates(candidates, client, key):
    cache = {}
    for row in candidates:
        address = row["후보주소"]
        if address not in cache:
            payload = client.request(VWORLD_URL, {"service": "address", "request": "getCoord", "version": "2.0",
                                     "crs": "EPSG:4326", "address": address, "format": "json", "type": "parcel"}, key, "key")
            try:
                response = json.loads(payload)["response"]
                status = text(response["status"])
                point = response.get("result", {}).get("point", {})
                lon, lat = number(point.get("x")), number(point.get("y"))
                if status == "OK" and lon is not None and lat is not None and 124 <= lon <= 132 and 32 <= lat <= 40:
                    cache[address] = {"경도": lon, "위도": lat, "좌표상태": "후보주소좌표_거래위치미확정"}
                elif status == "NOT_FOUND":
                    cache[address] = {"경도": None, "위도": None, "좌표상태": "주소검색결과없음"}
                else:
                    raise AnalysisError("VWorld 인증/응답 오류. 활용신청·키·응답을 확인하세요.")
            except (ValueError, KeyError, TypeError, AttributeError):
                raise AnalysisError("VWorld 응답 구조가 올바르지 않습니다.") from None
        row.update(cache[address])


def export_csv(path, rows, empty_columns):
    columns = list(dict.fromkeys(k for row in rows for k in row)) or empty_columns
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def export_sqlite(path, tables):
    with sqlite3.connect(path) as con:
        for table, (rows, defaults) in tables.items():
            columns = list(dict.fromkeys(k for row in rows for k in row)) or defaults
            def quoted(value):
                return '"' + value.replace('"', '""') + '"'
            declarations = []
            for col in columns:
                values = [r[col] for r in rows if r.get(col) is not None]
                dtype = "INTEGER" if values and all(isinstance(v, (int, bool)) for v in values) else (
                    "REAL" if values and all(isinstance(v, (float, int)) for v in values) else "TEXT")
                declarations.append(quoted(col) + " " + dtype)
            con.execute(f"CREATE TABLE {quoted(table)} ({', '.join(declarations)})")
            if rows:
                placeholders = ",".join("?" for _ in columns)
                con.executemany(f"INSERT INTO {quoted(table)} VALUES ({placeholders})",
                                [[r.get(col) for col in columns] for r in rows])
        con.execute('CREATE INDEX idx_candidates_trade ON address_candidates("거래ID")')


def export_results(output, trades, summaries, candidates, raw_trade, raw_building, metadata):
    output.mkdir(parents=True, exist_ok=False)
    tables = {
        "single_multi_house_trade": (trades, ["거래ID", "법정동", "지번_마스킹"]),
        "trade_summary": (summaries, ["거래ID", "매칭상태"]),
        "address_candidates": (candidates, ["거래ID", "후보순위", "후보주소", "유사도점수"]),
    }
    for name, (rows, defaults) in tables.items():
        export_csv(output / (name + ".csv"), rows, defaults)
    export_sqlite(output / "analysis.sqlite", tables)
    save_json(output / "raw_trades.json", raw_trade)
    save_json(output / "raw_building_titles.json", raw_building)
    save_json(output / "run_info.json", metadata)
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["경도"], r["위도"]]},
                 "properties": r} for r in candidates if r.get("경도") is not None and r.get("위도") is not None]
    if features:
        save_json(output / "qgis_candidates.geojson", {"type": "FeatureCollection", "features": features})


def validate_config(config):
    if not isinstance(config, dict):
        raise AnalysisError("설정 파일은 JSON 객체여야 합니다.")
    # 키를 설정 파일/메타데이터에 저장하는 실수를 명시적으로 차단한다.
    def inspect(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if re.search(r"key|token|secret|password", key, re.I):
                    raise AnalysisError("API 키는 설정 파일에 넣지 마세요. 실행 시 비공개 입력합니다.")
                inspect(value)
        elif isinstance(obj, list):
            for value in obj:
                inspect(value)
    inspect(config)
    if "periods" in config:
        if "start_month" in config or "end_month" in config:
            raise AnalysisError("여러 기간은 periods만 사용하고 최상위 start_month/end_month는 제거하세요.")
        periods = config["periods"]
        if not isinstance(periods, list) or not periods or any(not isinstance(p, dict) for p in periods):
            raise AnalysisError("periods는 시작월/종료월 객체를 한 개 이상 가진 목록이어야 합니다.")
        selected = sorted({m for p in periods for m in months(p.get("start_month", ""), p.get("end_month", ""))})
    else:
        selected = months(config.get("start_month", ""), config.get("end_month", ""))
    if selected[-1] > date.today().strftime("%Y%m"):
        raise AnalysisError("조회 종료월은 현재월 이후일 수 없습니다.")
    settings = config.get("matching", {})
    if not isinstance(settings, dict):
        raise AnalysisError("matching은 JSON 객체여야 합니다.")
    for name, default, low, high in [("top_n", 5, 1, 100), ("max_year_gap", 10, 0, 100),
                                      ("min_score", 40, 0, 100), ("ambiguity_gap", 5, 0, 100)]:
        value = settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise AnalysisError(f"matching.{name} 범위는 {low}~{high}입니다.")
        if name in {"top_n", "max_year_gap"} and not isinstance(value, int):
            raise AnalysisError(f"matching.{name}은 정수로 입력하세요.")
    if settings.get("mask_mode", "digit_count") not in {"digit_count", "wildcard"}:
        raise AnalysisError("mask_mode은 digit_count 또는 wildcard입니다.")
    if not isinstance(settings.get("include_cancelled", False), bool):
        raise AnalysisError("include_cancelled는 true/false입니다.")
    if not isinstance(config.get("geocode", False), bool):
        raise AnalysisError("geocode는 true/false입니다.")
    if not isinstance(config.get("areas"), list):
        raise AnalysisError("areas는 지역 객체 목록입니다.")
    for area in config["areas"]:
        if not isinstance(area, dict) or not isinstance(area.get("legal_dongs", []), list):
            raise AnalysisError("areas는 지역 객체 목록, legal_dongs는 법정동 객체 목록이어야 합니다.")
        if any(not isinstance(dong, dict) for dong in area.get("legal_dongs", [])):
            raise AnalysisError("legal_dongs의 각 항목은 name과 code를 가진 객체여야 합니다.")
    if not isinstance(config.get("output_dir", "results"), str) or not text(config.get("output_dir", "results")):
        raise AnalysisError("output_dir은 비어 있지 않은 경로 문자열이어야 합니다.")
    cache_hours = config.get("cache_hours", 24)
    if (isinstance(cache_hours, bool) or not isinstance(cache_hours, (int, float))
            or not math.isfinite(cache_hours) or cache_hours < 0):
        raise AnalysisError("cache_hours는 0 이상의 유한한 숫자입니다.")
    return selected


def prompt_key(label, allow_empty=False):
    value = getpass.getpass(label).strip()
    if not value and not allow_empty:
        raise AnalysisError("인증키를 입력하지 않았습니다.")
    return value


def run(config, config_dir, args):
    selected_months = validate_config(config)
    root = (config_dir / config.get("output_dir", "results")).resolve()
    cache_hours = config.get("cache_hours", 24)
    client = ApiClient(root / "cache", args.refresh, cache_hours)
    offline = bool(args.trade_source and args.building_source)
    if offline:
        key = ""
    else:
        key = prompt_key("공공데이터포털 인증키(화면에 표시하지 않음): ")
    # 로컬 분석은 코드 자동조회 요청 없이 동작하도록 코드 명시를 요구한다.
    if offline and any(not d.get("code") for a in config["areas"] for d in a.get("legal_dongs", [])):
        raise AnalysisError("로컬 자료만 분석할 때는 legal_dongs의 10자리 code를 지정하세요.")
    scopes = resolve_scopes(config, client, key, config_dir)
    if args.trade_source:
        raw_trade = read_source(args.trade_source, args.trade_table)
    else:
        raw_trade = []
        for sgg in sorted({s["code"][:5] for s in scopes}):
            for month in selected_months:
                print(f"[실거래가] 시군구 {sgg}, {month}")
                rows = client.collect(TRADE_URL, {"LAWD_CD": sgg, "DEAL_YMD": month}, key)
                raw_trade.extend({**r, "_sgg": sgg, "_month": month} for r in rows)
    trades = make_trade_rows(raw_trade, scopes, selected_months)
    if args.building_source:
        raw_building = read_source(args.building_source, args.building_table)
    else:
        building_key = prompt_key("건축물대장 인증키(같은 키면 Enter): ", allow_empty=True) or key
        raw_building = []
        for scope in scopes:
            print(f'[건축물대장] {scope["sigungu"]} {scope["name"]}')
            rows = client.collect(BUILDING_URL, {"sigunguCd": scope["code"][:5],
                                  "bjdongCd": scope["code"][5:], "_type": "json"}, building_key)
            raw_building.extend({**r, "_sgg": scope["code"][:5], "_bjd": scope["code"][5:]} for r in rows)
    buildings = make_buildings(raw_building, scopes)
    summaries, candidates = match_trades(trades, buildings, config.get("matching", {}))
    geocode_status = "사용안함"
    if config.get("geocode") and candidates:
        geo_key = prompt_key("VWorld 인증키(공공데이터포털 키와 별개): ")
        try:
            add_coordinates(candidates, client, geo_key)
            geocode_status = "완료"
        except AnalysisError as exc:
            # 선택 기능이 실패해도 수집·매칭 결과는 저장한다. CLI 종료코드는 실패로 유지.
            geocode_status = str(exc)
            for row in candidates:
                row.setdefault("좌표상태", "좌표처리미완료")
    warnings = []
    if not trades:
        warnings.append("선택 조건의 거래가 0건입니다. 지역·기간·입력 자료 범위를 확인하세요.")
    if not buildings:
        warnings.append("비교 가능한 일반 단독·다가구 표제부가 0건입니다.")
    output = root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    metadata = {
        "실행시각_UTC": now(), "설정": config, "법정동범위": scopes,
        "거래건수": len(trades), "비교지번수": len(buildings), "후보행수": len(candidates),
        "매칭상태별건수": dict(Counter(r["매칭상태"] for r in summaries)),
        "API수집기록": client.audit,
        "기존자료입력": {"실거래가": text(args.trade_source), "건축물대장": text(args.building_source)},
        "좌표처리": geocode_status, "경고": warnings,
        "주의사항": NOTICE + " 현재 대장으로 과거 거래를 비교하므로 철거·분할·증축·지분거래는 식별에 한계가 있습니다.",
    }
    export_results(output, trades, summaries, candidates, raw_trade, raw_building, metadata)
    print(f"저장 완료: {output}\n거래 {len(trades)}건 / 후보 {len(candidates)}행")
    for warning in warnings:
        print("[주의] " + warning)
    print(NOTICE)
    return output, 0 if geocode_status in {"사용안함", "완료"} else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=BASE / "config.json", help="조회 설정 JSON")
    parser.add_argument("--trade-source", type=Path, help="기존 실거래가 CSV/SQLite (생략하면 API 수집)")
    parser.add_argument("--building-source", type=Path, help="기존 표제부 CSV/SQLite (생략하면 API 수집)")
    parser.add_argument("--trade-table", default="single_multi_house_trade")
    parser.add_argument("--building-table", default="building_title_all")
    parser.add_argument("--refresh", action="store_true", help="유효 캐시도 무시하고 재수집")
    parser.add_argument("--demo", action="store_true", help="합성 예제로 키/외부통신 없이 실행")
    args = parser.parse_args(argv)
    if args.demo:
        args.config = BASE / "examples" / "demo_config.json"
        args.trade_source = BASE / "examples" / "demo_trades.csv"
        args.building_source = BASE / "examples" / "demo_buildings.csv"
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        _, exit_code = run(config, args.config.resolve().parent, args)
        return exit_code
    except (AnalysisError, FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, sqlite3.Error) as exc:
        print("[오류] " + str(exc), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\n입력이 취소되었습니다. 인증키는 저장하지 않았습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
