"""외부 API/실제 주소를 사용하지 않는 회귀 테스트."""
import argparse
import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import single_multi_house as app


ROOT = Path(app.__file__).parent


def page(rows, total=None, page_no=1):
    return json.dumps({"response": {"header": {"resultCode": "00"}, "body": {
        "items": {"item": rows}, "totalCount": len(rows) if total is None else total,
        "pageNo": page_no}}})


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = json.loads((ROOT / "examples/demo_config.json").read_text())
        self.client = app.ApiClient(self.directory / "cache", delay=0)
        self.scopes = app.resolve_scopes(self.config, self.client, "", ROOT)
        self.raw_trades = app.read_csv(ROOT / "examples/demo_trades.csv")
        self.raw_buildings = app.read_csv(ROOT / "examples/demo_buildings.csv")
        self.trades = app.make_trade_rows(self.raw_trades, self.scopes, ["202501", "202502"])
        self.buildings = app.make_buildings(self.raw_buildings, self.scopes)

    def test_inclusive_months_across_year(self):
        self.assertEqual(app.months("202412", "202502"), ["202412", "202501", "202502"])

    def test_invalid_months(self):
        for first, last in [("202513", "202601"), ("202501", "202412"), ("20251", "202502")]:
            with self.subTest(first=first), self.assertRaises(app.AnalysisError):
                app.months(first, last)

    def test_multiple_periods_are_unioned_without_repeated_months(self):
        del self.config["start_month"], self.config["end_month"]
        self.config["periods"] = [{"start_month": "202412", "end_month": "202501"},
                                  {"start_month": "202501", "end_month": "202502"},
                                  {"start_month": "202505", "end_month": "202505"}]
        self.assertEqual(app.validate_config(self.config), ["202412", "202501", "202502", "202505"])

    def test_multiple_periods_do_not_silently_override_default_period(self):
        self.config["periods"] = [{"start_month": "202501", "end_month": "202502"}]
        with self.assertRaises(app.AnalysisError):
            app.validate_config(self.config)

    def test_config_rejects_credentials(self):
        self.config["service_key"] = "DO-NOT-STORE-TEST-CREDENTIAL"
        with self.assertRaises(app.AnalysisError) as caught:
            app.validate_config(self.config)
        self.assertNotIn("DO-NOT-STORE", str(caught.exception))

    def test_config_rejects_invalid_top_n_and_bool(self):
        for value in [0, 2.5, True, 101]:
            self.config["matching"]["top_n"] = value
            with self.subTest(value=value), self.assertRaises(app.AnalysisError):
                app.validate_config(self.config)

    def test_scope_rejects_cross_region_code(self):
        self.config["areas"][0]["sigungu_code"] = "88888"
        with self.assertRaises(app.AnalysisError):
            app.resolve_scopes(self.config, self.client, "", ROOT)

    def test_scope_deduplicates(self):
        self.config["areas"].append(copy.deepcopy(self.config["areas"][0]))
        self.assertEqual(len(app.resolve_scopes(self.config, self.client, "", ROOT)), 2)

    def test_scope_never_confuses_administrative_dong(self):
        self.config["areas"][0]["administrative_dongs"] = ["예시1동"]
        with self.assertRaises(app.AnalysisError):
            app.resolve_scopes(self.config, self.client, "", ROOT)

    def test_masked_parcels(self):
        cases = [("12*", "123-4", True), ("12*", "1234", False), ("**", "12", True),
                 ("1*3", "123", True), ("12*-*", "123-4", True), ("산1*", "산12", True),
                 ("산1*", "12", False), ("1*", "산12", False), ("123", "123-4", False),
                 ("123-0", "123", True), ("0123-0004", "123-4", True), ("", "123", False),
                 ("12*", "12*", False), ("1*", "1", False)]
        for masked, actual, expected in cases:
            with self.subTest(mask=masked, candidate=actual):
                self.assertEqual(app.parcel_matches(masked, actual), expected)

    def test_explicit_wildcard_mode(self):
        self.assertFalse(app.parcel_matches("12*", "12345"))
        self.assertTrue(app.parcel_matches("12*", "12345", "wildcard"))

    def test_dates_filter_and_exact_duplicate_transactions_preserved(self):
        self.assertEqual(len(self.trades), 7)
        self.assertEqual(len({r["거래ID"] for r in self.trades}), 7)
        self.assertNotEqual(self.trades[0]["거래ID"], self.trades[1]["거래ID"])
        self.assertEqual(self.trades[0]["거래금액_원"], 1000000000)
        self.assertEqual(self.trades[0]["계약당시_경과연수"], 25)

    def test_invalid_contract_date_is_not_silently_lost(self):
        self.raw_trades[0]["dealDay"] = "32"
        with self.assertRaises(app.AnalysisError):
            app.make_trade_rows(self.raw_trades, self.scopes, ["202501"])

    def test_trade_code_conflict_rejected(self):
        self.raw_trades[0]["법정동코드"] = "8888810100"
        with self.assertRaises(app.AnalysisError):
            app.make_trade_rows(self.raw_trades, self.scopes, ["202501"])

    def test_collective_buildings_are_excluded(self):
        self.assertEqual(len(self.buildings), 8)
        self.assertFalse(any(r["관리번호"] == "SYNTHETIC-9" for r in self.buildings))

    def test_multiple_buildings_sum_floor_but_not_duplicate_land(self):
        first = copy.deepcopy(self.raw_buildings[0])
        second = {**first, "mgmBldrgstPk": "OTHER", "totArea": "50"}
        parcels = app.make_buildings([first, second, first], self.scopes)
        self.assertEqual(len(parcels), 1)
        self.assertEqual(parcels[0]["연면적"], 250)
        self.assertEqual(parcels[0]["대지면적"], 100)
        self.assertEqual(parcels[0]["대장동수"], 2)
        self.assertTrue(parcels[0]["대장경고"])

    def test_conflicting_or_missing_building_area_is_not_invented(self):
        first = copy.deepcopy(self.raw_buildings[0])
        second = {**first, "mgmBldrgstPk": "OTHER", "totArea": "", "platArea": "50"}
        result = app.make_buildings([first, second], self.scopes)[0]
        self.assertIsNone(result["연면적"])
        self.assertIsNone(result["대지면적"])

    def test_legacy_mountain_parcel_uses_land_type_code(self):
        raw = {**self.raw_buildings[5], "jibun": "12"}
        result = app.make_buildings([raw], self.scopes)[0]
        self.assertEqual(result["지번"], "산12")
        self.assertFalse(app.parcel_matches("1*", result["지번"]))

    def test_summary_register_not_double_counted(self):
        raw = {**self.raw_buildings[0], "regstrKindCd": "1"}
        self.assertEqual(app.make_buildings([raw], self.scopes), [])

    def test_config_shape_errors_are_reported_before_key_input(self):
        changes = [{"matching": []}, {"areas": ["wrong"]}, {"output_dir": ""}, {"cache_hours": float("nan")}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(app.AnalysisError):
                app.validate_config({**self.config, **change})

    def test_matching_keeps_region_boundaries(self):
        summaries, candidates = app.match_trades(self.trades, self.buildings, self.config["matching"])
        self.assertEqual(len(summaries), 7)
        for row in candidates:
            expected = "다른구" if row["지역코드"] == "88888" else "예시구"
            self.assertIn(expected, row["후보주소"])
        self.assertEqual(summaries[0]["1순위후보주소"], "가상도 예시구 예시동 123-4")

    def test_ties_detected_before_top_n_truncation(self):
        self.config["matching"]["top_n"] = 1
        summaries, candidates = app.match_trades(self.trades, self.buildings, self.config["matching"])
        self.assertEqual(summaries[2]["매칭상태"], "복수후보_구분어려움")
        self.assertEqual(summaries[2]["기준통과후보수"], 2)
        self.assertEqual(summaries[2]["1_2순위점수차"], 0)
        self.assertEqual(sum(r["거래ID"] == self.trades[2]["거래ID"] for r in candidates), 1)

    def test_cancellations_preserved_but_excluded_by_default(self):
        summaries, candidates = app.match_trades(self.trades, self.buildings, {})
        self.assertEqual(summaries[3]["매칭상태"], "취소거래_분석제외")
        self.assertFalse(any(r["거래ID"] == self.trades[3]["거래ID"] for r in candidates))
        summaries, _ = app.match_trades(self.trades, self.buildings, {"include_cancelled": True})
        self.assertIn("1순위후보주소", summaries[3])

    def test_missing_values_do_not_get_perfect_score(self):
        trade = {**self.trades[0], "연면적_㎡": None, "대지면적_㎡": None, "건축년도": None}
        detail = app.score_candidate(trade, self.buildings[0])
        self.assertEqual(detail["유사도점수"], 10)
        self.assertEqual(detail["비교가능가중치"], 10)

    def test_fully_masked_parcel_is_not_high_confidence(self):
        trade = {**self.trades[0], "지번_마스킹": "***"}
        summaries, _ = app.match_trades([trade], [self.buildings[0]], {})
        self.assertEqual(summaries[0]["매칭상태"], "추정후보_검증필요")

    def test_empty_match_still_preserves_trade(self):
        summaries, candidates = app.match_trades(self.trades, [], {})
        self.assertEqual(len(summaries), 7)
        self.assertEqual(candidates, [])
        self.assertEqual(summaries[0]["미매칭사유"], "해당법정동_대장없음")

    def test_xml_and_json_page_parsing(self):
        xml = '<response><header><resultCode>000</resultCode></header><body><items><item><jibun>1*</jibun></item></items><totalCount>1</totalCount><pageNo>1</pageNo></body></response>'
        self.assertEqual(app.parse_page(xml), ([{"jibun": "1*"}], 1, 1))
        self.assertEqual(app.parse_page(page({"id": "1"}, 1)), ([{"id": "1"}], 1, 1))
        self.assertEqual(app.parse_page(page([], 0)), ([], 0, 1))

    def test_api_errors_are_not_reported_as_empty_success(self):
        for payload in ['<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>',
                        '<html>Error</html>', '{"response":{"header":{"resultCode":"30"}}}',
                        '{"response":{"header":{"resultCode":"00"},"body":{}}}']:
            with self.subTest(payload=payload), self.assertRaises(app.AnalysisError):
                app.parse_page(payload)

    def test_pagination_short_page_cache_and_refresh(self):
        with patch.object(self.client, "request", side_effect=[page([{"n": 1}], 2), page([{"n": 2}], 2, 2)]) as request:
            rows = self.client.collect(app.TRADE_URL, {"LAWD_CD": "99999"}, "FAKE-KEY")
            self.assertEqual(len(rows), 2)
            self.assertEqual(request.call_count, 2)
        with patch.object(self.client, "request", side_effect=AssertionError("must use cache")):
            self.assertEqual(self.client.collect(app.TRADE_URL, {"LAWD_CD": "99999"}, "ANOTHER-KEY"), rows)
        self.assertTrue(self.client.audit[-1]["캐시사용"])
        cache_text = next((self.directory / "cache").glob("*.json")).read_text()
        self.assertNotIn("FAKE-KEY", cache_text)
        self.client.refresh = True
        with patch.object(self.client, "request", return_value=page([], 0)) as request:
            self.assertEqual(self.client.collect(app.TRADE_URL, {"LAWD_CD": "99999"}, "FAKE-KEY"), [])
            request.assert_called_once()

    def test_incomplete_collection_and_repeated_page_not_cached(self):
        failures = [[page([{"n": 1}], 2), page([], 2, 2)],
                    [page([{"n": 1}], 2), page([{"n": 1}], 2, 2)],
                    [page([{"n": 1}], 2), page([{"n": 2}], 3, 2)],
                    [page([{"n": 1}], 1, 5)]]
        for responses in failures:
            with self.subTest(responses=responses), patch.object(self.client, "request", side_effect=responses):
                with self.assertRaises(app.AnalysisError):
                    self.client.collect(app.TRADE_URL, {}, "FAKE-KEY")
                self.assertFalse(list((self.directory / "cache").glob("*.json")))

    def test_secret_encoding_and_http_error_redaction(self):
        requests = []
        def reject(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 403, "FAKE-RAW-KEY", {}, None)
        with patch("urllib.request.urlopen", side_effect=reject):
            with self.assertRaises(app.AnalysisError) as error:
                self.client.request(app.TRADE_URL, {}, "a%2Bb%2Fc%3D")
        query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(requests[0].full_url.split("?", 1)[1])
        self.assertEqual(query["serviceKey"], ["a+b/c="])
        self.assertNotIn("a+b", str(error.exception))
        self.assertNotIn("FAKE-RAW", str(error.exception))

    def test_legal_code_lookup_requires_exact_unique_official_name(self):
        def legal(rows):
            return json.dumps({"StanReginCd": [{"head": [{"totalCount": len(rows)}, {"RESULT": {"resultCode": "INFO-0"}}]}, {"row": rows}]})
        rows = [{"region_cd": "9999910100", "locatadd_nm": "가상도 예시구 예시동"},
                {"region_cd": "8888810100", "locatadd_nm": "가상도 다른구 예시동"}]
        with patch.object(self.client, "request", return_value=legal(rows)):
            self.assertEqual(self.client.lookup_legal("가상도 예시구 예시동", "FAKE-KEY"), "9999910100")
        rows.append({"region_cd": "7777710100", "locatadd_nm": "가상도 예시구 예시동"})
        with patch.object(self.client, "request", return_value=legal(rows)), self.assertRaises(app.AnalysisError):
            self.client.lookup_legal("가상도 예시구 예시동", "FAKE-KEY")

    def test_geocoding_and_export_marks_candidate_not_verified_trade(self):
        rows = [{"후보주소": "가상 주소"}, {"후보주소": "가상 주소"}]
        response = json.dumps({"response": {"status": "OK", "result": {"point": {"x": "127", "y": "37"}}}})
        with patch.object(self.client, "request", return_value=response) as request:
            app.add_coordinates(rows, self.client, "FAKE-GEO-KEY")
            self.assertEqual(request.call_count, 1)
        self.assertEqual(rows[0]["경도"], 127)
        self.assertIn("거래위치미확정", rows[0]["좌표상태"])

    def test_existing_sqlite_and_csv_roundtrip(self):
        tables = {"single_multi_house_trade": (self.trades, ["거래ID"]),
                  "address_candidates": ([], ["거래ID", "후보순위"])}
        target = self.directory / "roundtrip.sqlite"
        app.export_sqlite(target, tables)
        rows = app.read_source(target, "single_multi_house_trade")
        again = app.make_trade_rows(rows, self.scopes, ["202501", "202502"])
        self.assertEqual(len(again), 7)
        self.assertEqual(again[0]["거래금액_만원"], self.trades[0]["거래금액_만원"])
        with self.assertRaises(app.AnalysisError):
            app.read_source(self.directory / "does-not-exist.sqlite", "missing")
        self.assertFalse((self.directory / "does-not-exist.sqlite").exists())

    def test_end_to_end_local_mode_repeatable_without_api_keys(self):
        self.config["output_dir"] = str(self.directory / "results")
        args = argparse.Namespace(refresh=False, trade_source=ROOT / "examples/demo_trades.csv",
                                  building_source=ROOT / "examples/demo_buildings.csv",
                                  trade_table="single_multi_house_trade", building_table="building_title_all")
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network allowed")), \
                patch.object(app, "prompt_key", side_effect=AssertionError("no key needed")), \
                contextlib.redirect_stdout(io.StringIO()):
            first, status = app.run(self.config, ROOT, args)
            second, _ = app.run(self.config, ROOT, args)
        self.assertEqual(status, 0)
        self.assertNotEqual(first, second)
        summary = app.read_csv(first / "trade_summary.csv")
        self.assertEqual(len(summary), 7)
        info = json.loads((first / "run_info.json").read_text())
        self.assertEqual(info["거래건수"], 7)
        self.assertEqual(info["후보행수"], 8)
        with sqlite3.connect(first / "analysis.sqlite") as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM single_multi_house_trade").fetchone()[0], 7)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM address_candidates").fetchone()[0], 8)
            self.assertEqual(con.execute('SELECT typeof("유사도점수") FROM address_candidates LIMIT 1').fetchone()[0], "real")

    def test_end_to_end_api_flow_with_mock_responses(self):
        self.config["output_dir"] = str(self.directory / "api_results")
        args = argparse.Namespace(refresh=False, trade_source=None, building_source=None,
                                  trade_table="single_multi_house_trade", building_table="building_title_all")
        def response(client, url, params, secret, secret_param="serviceKey"):
            self.assertEqual(secret, "FAKE-RUNTIME-SECRET")
            self.assertEqual(params["pageNo"], 1)
            if url == app.TRADE_URL:
                rows = [r for r in self.raw_trades if r["sggCd"] == params["LAWD_CD"]
                        and f'{r["dealYear"]}{int(r["dealMonth"]):02d}' == params["DEAL_YMD"]]
            elif url == app.BUILDING_URL:
                rows = [r for r in self.raw_buildings if r["sigunguCd"] == params["sigunguCd"]
                        and r["bjdongCd"] == params["bjdongCd"]]
            else:
                raise AssertionError("Unexpected URL")
            return page(rows)
        with patch.object(app.ApiClient, "request", new=response), \
                patch.object(app, "prompt_key", return_value="FAKE-RUNTIME-SECRET"), \
                contextlib.redirect_stdout(io.StringIO()):
            output, status = app.run(self.config, ROOT, args)
        info = json.loads((output / "run_info.json").read_text())
        self.assertEqual(status, 0)
        self.assertEqual(info["거래건수"], 7)
        self.assertEqual(info["후보행수"], 8)
        self.assertEqual(len(info["API수집기록"]), 6)  # 시군구 2개 × 2개월 + 법정동 표제부 2개
        for path in (self.directory / "api_results").rglob("*"):
            if path.suffix in {".json", ".csv"}:
                self.assertNotIn("FAKE-RUNTIME-SECRET", path.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
