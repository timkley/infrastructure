from __future__ import annotations

import json
import os
import re
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("HEISENBERG_ACCESS_MCP_TOKEN", "test-token")

from heisenberg_access_mcp.server import (
    CapabilityError,
    GOOGLE_HEALTH_MEAL_TYPES,
    GOOGLE_HEALTH_NUTRITION_DATA_POINT_ID_RE,
    GOOGLE_HEALTH_NUTRITION_READ_SCOPE,
    GOOGLE_HEALTH_NUTRITION_WRITE_SCOPE,
    build_google_health_nutrition_data_point,
    build_mcp,
    correct_google_health_nutrition_item,
    create_google_health_meal,
    ensure_write_confirmed,
    google_health_activity_data_types_payload,
    google_health_batch_delete_nutrition_data_points,
    google_health_list_nutrition_items,
    google_health_nutrition_range_end_exclusive,
    google_health_nutrition_data_point_id_from_name,
    google_health_nutrition_item_from_data_point,
    google_health_operation_summary,
    new_google_health_nutrition_data_point_id,
    normalize_google_health_meal,
    normalize_google_health_nutrition_correction_changes,
    normalize_google_health_nutrition_data_point_ids,
    normalize_google_health_nutrition_timestamp,
    summarize_google_health_nutrition_day,
    summarize_google_health_nutrition_range,
)


REAL_ASYNC_CLIENT = httpx.AsyncClient


def nutrition_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "display_name": "Skyr, 250 g",
        "energy_kcal": 160,
        "protein_g": 27.5,
        "carbohydrate_g": 10,
        "fat_g": 0.5,
    }
    item.update(overrides)
    return item


def nutrition_data_point(
    *,
    data_point_id: str = "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    timestamp: str = "2026-03-29T11:03:00+02:00",
    meal_type: str = "BEFORE_LUNCH",
    item: dict[str, object] | None = None,
) -> dict[str, object]:
    meal = normalize_google_health_meal(timestamp, meal_type, [item or nutrition_item()])
    _, payload = build_google_health_nutrition_data_point(
        timestamp=meal["timestamp"],
        utc_offset=meal["utc_offset"],
        meal_type=meal["meal_type"],
        item=meal["items"][0],
        data_point_id=data_point_id,
    )
    return payload


class GoogleHealthNutritionValidationTest(unittest.TestCase):
    def test_timestamp_requires_rfc3339_offset(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "nutrition_timestamp_must_be_rfc3339_with_offset"):
            normalize_google_health_nutrition_timestamp("2026-03-29T11:03:00")

        timestamp, offset = normalize_google_health_nutrition_timestamp("2026-03-29T11:03:00+02:00")
        self.assertEqual(timestamp, "2026-03-29T11:03:00+02:00")
        self.assertEqual(offset, "7200s")

    def test_full_google_meal_type_enum_is_supported(self) -> None:
        self.assertEqual(
            GOOGLE_HEALTH_MEAL_TYPES,
            {
                "AFTER_DINNER",
                "ANYTIME",
                "BEFORE_BREAKFAST",
                "BEFORE_DINNER",
                "BEFORE_LUNCH",
                "BREAKFAST",
                "DINNER",
                "LUNCH",
                "SNACK",
            },
        )
        for meal_type in GOOGLE_HEALTH_MEAL_TYPES:
            meal = normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                meal_type,
                [nutrition_item()],
            )
            self.assertEqual(meal["meal_type"], meal_type)

        with self.assertRaisesRegex(CapabilityError, "meal_type_not_allowed"):
            normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                "BRUNCH",
                [nutrition_item()],
            )

    def test_core_macros_are_required_and_not_estimated(self) -> None:
        incomplete = nutrition_item()
        del incomplete["protein_g"]
        with self.assertRaisesRegex(CapabilityError, "items_0_missing_required_fields"):
            normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                "BREAKFAST",
                [incomplete],
            )

    def test_negative_nan_and_unknown_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "items_0_energy_kcal_must_be_non_negative_finite_number"):
            normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                "BREAKFAST",
                [nutrition_item(energy_kcal=float("nan"))],
            )
        with self.assertRaisesRegex(CapabilityError, "items_0_contains_unsupported_fields"):
            normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                "BREAKFAST",
                [nutrition_item(calories=160)],
            )

    def test_additional_nutrients_use_google_enum_and_grams(self) -> None:
        meal = normalize_google_health_meal(
            "2026-03-29T11:03:00+02:00",
            "BREAKFAST",
            [nutrition_item(additional_nutrients_g={"DIETARY_FIBER": 4.2, "SODIUM": 0.12})],
        )
        self.assertEqual(
            meal["items"][0]["additional_nutrients_g"],
            {"DIETARY_FIBER": 4.2, "SODIUM": 0.12},
        )
        with self.assertRaisesRegex(CapabilityError, "duplicates_core_macro"):
            normalize_google_health_meal(
                "2026-03-29T11:03:00+02:00",
                "BREAKFAST",
                [nutrition_item(additional_nutrients_g={"PROTEIN": 1})],
            )

    def test_confirmation_is_required_for_writes(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "confirmation_required_for_mutating_method"):
            ensure_write_confirmed("POST", False)
        ensure_write_confirmed("POST", True)

    def test_nutrition_range_is_bounded_and_overflow_safe(self) -> None:
        self.assertEqual(
            google_health_nutrition_range_end_exclusive(date(2026, 1, 1), date(2026, 3, 31)),
            date(2026, 4, 1),
        )
        with self.assertRaisesRegex(CapabilityError, "nutrition_date_range_too_large"):
            google_health_nutrition_range_end_exclusive(date(2026, 1, 1), date(2026, 4, 1))
        with self.assertRaisesRegex(CapabilityError, "nutrition_end_date_too_late"):
            google_health_nutrition_range_end_exclusive(date.max, date.max)


class GoogleHealthNutritionPayloadTest(unittest.TestCase):
    def test_anonymous_payload_matches_google_shapes(self) -> None:
        meal = normalize_google_health_meal(
            "2026-03-29T11:03:00+02:00",
            "BEFORE_LUNCH",
            [
                nutrition_item(
                    energy_from_fat_kcal=4.5,
                    additional_nutrients_g={"DIETARY_FIBER": 3.5},
                )
            ],
        )
        data_point_id, payload = build_google_health_nutrition_data_point(
            timestamp=meal["timestamp"],
            utc_offset=meal["utc_offset"],
            meal_type=meal["meal_type"],
            item=meal["items"][0],
        )

        self.assertRegex(data_point_id, GOOGLE_HEALTH_NUTRITION_DATA_POINT_ID_RE)
        self.assertEqual(payload["name"], f"users/me/dataTypes/nutrition-log/dataPoints/{data_point_id}")
        log = payload["nutritionLog"]
        self.assertNotIn("food", log)
        self.assertNotIn("serving", log)
        self.assertEqual(log["foodDisplayName"], "Skyr, 250 g")
        self.assertEqual(
            log["interval"],
            {
                "startTime": "2026-03-29T11:03:00+02:00",
                "startUtcOffset": "7200s",
                "endTime": "2026-03-29T11:03:01+02:00",
                "endUtcOffset": "7200s",
            },
        )
        self.assertEqual(log["energy"], {"kcal": 160.0, "userProvidedUnit": "KILOCALORIE"})
        self.assertEqual(log["totalCarbohydrate"], {"grams": 10.0, "userProvidedUnit": "GRAM"})
        self.assertEqual(log["totalFat"], {"grams": 0.5, "userProvidedUnit": "GRAM"})
        self.assertEqual(log["energyFromFat"], {"kcal": 4.5, "userProvidedUnit": "KILOCALORIE"})
        self.assertEqual(
            log["nutrients"],
            [
                {"quantity": {"grams": 27.5, "userProvidedUnit": "GRAM"}, "nutrient": "PROTEIN"},
                {"quantity": {"grams": 3.5, "userProvidedUnit": "GRAM"}, "nutrient": "DIETARY_FIBER"},
            ],
        )

    def test_generated_ids_are_nondeterministic_and_google_compatible(self) -> None:
        first = new_google_health_nutrition_data_point_id()
        second = new_google_health_nutrition_data_point_id()
        self.assertNotEqual(first, second)
        self.assertTrue(4 <= len(first) <= 63)
        self.assertRegex(first, re.compile(r"^[a-z0-9-]+$"))

    def test_completed_create_operation_exposes_google_assigned_id(self) -> None:
        operation = google_health_operation_summary(
            {
                "done": True,
                "response": {
                    "@type": "type.googleapis.com/google.devicesandservices.health.v4.DataPoint",
                    "name": "users/123/dataTypes/nutrition-log/dataPoints/2678973737190640032",
                },
            }
        )

        self.assertEqual(operation["data_point_id"], "2678973737190640032")
        self.assertFalse(operation["pending"])

    def test_point_parser_reconstructs_local_offset_and_exposes_only_id(self) -> None:
        payload = nutrition_data_point()
        log = payload["nutritionLog"]
        log["interval"]["startTime"] = "2026-03-29T09:03:00Z"
        parsed = google_health_nutrition_item_from_data_point(payload)

        self.assertEqual(parsed["data_point_id"], "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(parsed["datetime"], "2026-03-29T11:03:00+02:00")
        self.assertNotIn("name", parsed)
        self.assertEqual(
            google_health_nutrition_data_point_id_from_name(payload["name"]),
            "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_point_parser_defaults_google_omitted_zero_macros(self) -> None:
        payload = nutrition_data_point(
            item=nutrition_item(
                energy_kcal=0,
                protein_g=0,
                carbohydrate_g=0,
                fat_g=0,
            )
        )
        log = payload["nutritionLog"]
        log.pop("energy")
        log.pop("totalCarbohydrate")
        log.pop("totalFat")
        log["nutrients"] = []

        parsed = google_health_nutrition_item_from_data_point(payload)

        self.assertEqual(parsed["energy_kcal"], 0.0)
        self.assertEqual(parsed["protein_g"], 0.0)
        self.assertEqual(parsed["carbohydrate_g"], 0.0)
        self.assertEqual(parsed["fat_g"], 0.0)


class GoogleHealthNutritionSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            google_health_nutrition_item_from_data_point(
                nutrition_data_point(
                    data_point_id="meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    timestamp="2026-03-29T08:00:00+02:00",
                    meal_type="BREAKFAST",
                    item=nutrition_item(display_name="Skyr, 250 g", energy_kcal=160, protein_g=27.5),
                )
            ),
            google_health_nutrition_item_from_data_point(
                nutrition_data_point(
                    data_point_id="meal-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    timestamp="2026-03-29T08:00:00+02:00",
                    meal_type="BREAKFAST",
                    item=nutrition_item(display_name="Banane, 1 Stück", energy_kcal=100, protein_g=1.0),
                )
            ),
            google_health_nutrition_item_from_data_point(
                nutrition_data_point(
                    data_point_id="meal-cccccccccccccccccccccccccccccccc",
                    timestamp="2026-03-30T13:15:00+02:00",
                    meal_type="LUNCH",
                    item=nutrition_item(display_name="Bowl, 1 Portion", energy_kcal=500, protein_g=20),
                )
            ),
        ]

    def test_day_has_items_meal_groups_and_totals(self) -> None:
        summary = summarize_google_health_nutrition_day(date(2026, 3, 29), self.items[:2])
        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(summary["meal_count"], 1)
        self.assertEqual(summary["totals"]["energy_kcal"], 260.0)
        self.assertEqual(summary["totals"]["protein_g"], 28.5)
        self.assertEqual(summary["meals"][0]["data_point_ids"], [
            "meal-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ])
        self.assertEqual(len(summary["items"]), 2)

    def test_range_is_compact_and_includes_empty_days(self) -> None:
        days = summarize_google_health_nutrition_range(
            date(2026, 3, 29),
            date(2026, 3, 31),
            self.items,
        )
        self.assertEqual([day["date"] for day in days], ["2026-03-29", "2026-03-30", "2026-03-31"])
        self.assertEqual(days[0]["totals"]["energy_kcal"], 260.0)
        self.assertEqual(days[2]["item_count"], 0)
        self.assertNotIn("items", days[0])
        self.assertNotIn("data_point_ids", days[0]["meals"][0])


class GoogleHealthNutritionHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_paginates_and_sends_nutrition_civil_filter(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("pageToken") == "next":
                payload = {"dataPoints": [nutrition_data_point(data_point_id="meal-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")]}
            else:
                payload = {
                    "dataPoints": [nutrition_data_point()],
                    "nextPageToken": "next",
                }
            return httpx.Response(200, json=payload, request=request)

        transport = httpx.MockTransport(handler)
        with patch(
            "heisenberg_access_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: REAL_ASYNC_CLIENT(transport=transport),
        ):
            items = await google_health_list_nutrition_items(
                "access-token",
                start_date=date(2026, 3, 29),
                end_date_exclusive=date(2026, 3, 30),
            )

        self.assertEqual(len(items), 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.params["pageSize"], "10000")
        self.assertEqual(
            requests[0].url.params["filter"],
            'nutrition_log.interval.civil_start_time >= "2026-03-29" '
            'AND nutrition_log.interval.civil_start_time < "2026-03-30"',
        )

    async def test_read_error_is_sanitized(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"error": {"message": "denied", "access_token": "must-not-leak"}},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with patch(
            "heisenberg_access_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: REAL_ASYNC_CLIENT(transport=transport),
        ):
            with self.assertRaises(CapabilityError) as caught:
                await google_health_list_nutrition_items(
                    "access-token",
                    start_date=date(2026, 3, 29),
                    end_date_exclusive=date(2026, 3, 30),
                )

        self.assertEqual(caught.exception.code, "google_health_nutrition_read_failed")
        self.assertEqual(caught.exception.details["http_status"], 403)
        self.assertNotIn("must-not-leak", json.dumps(caught.exception.details))

    async def test_batch_delete_uses_full_names(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.content)
            return httpx.Response(200, json={"name": "operations/delete-1", "done": False}, request=request)

        transport = httpx.MockTransport(handler)
        with patch(
            "heisenberg_access_mcp.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: REAL_ASYNC_CLIENT(transport=transport),
        ):
            operation = await google_health_batch_delete_nutrition_data_points(
                "access-token",
                ["meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "meal-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
            )

        self.assertEqual(
            captured["url"],
            "https://health.googleapis.com/v4/users/me/dataTypes/nutrition-log/dataPoints:batchDelete",
        )
        self.assertEqual(
            captured["json"],
            {
                "names": [
                    "users/me/dataTypes/nutrition-log/dataPoints/meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "users/me/dataTypes/nutrition-log/dataPoints/meal-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                ]
            },
        )
        self.assertTrue(operation["pending"])


class GoogleHealthNutritionWriteFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_validates_without_oauth_and_unconfirmed_write_is_refused(self) -> None:
        mcp = build_mcp()
        log_tool = mcp._tool_manager.get_tool("google_health.log_meal")
        correction_tool = mcp._tool_manager.get_tool("google_health.correct_nutrition_item")
        delete_tool = mcp._tool_manager.get_tool("google_health.delete_nutrition_items")
        assert log_tool is not None and correction_tool is not None and delete_tool is not None
        refresh_mock = AsyncMock()
        with patch("heisenberg_access_mcp.server.refresh_google_access_token", new=refresh_mock):
            dry_run = await log_tool.fn(
                None,
                "2026-03-29T08:00:00+02:00",
                "BREAKFAST",
                [nutrition_item()],
                False,
                True,
            )
            refused = await log_tool.fn(
                None,
                "2026-03-29T08:00:00+02:00",
                "BREAKFAST",
                [nutrition_item()],
                False,
                False,
            )
            correction_dry_run = await correction_tool.fn(
                None,
                "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                {"energy_kcal": 175},
                False,
                True,
            )
            delete_dry_run = await delete_tool.fn(
                None,
                ["meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                False,
                True,
            )

        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["item_count"], 1)
        self.assertEqual(refused["error"], "confirmation_required_for_mutating_method")
        self.assertTrue(correction_dry_run["dry_run"])
        self.assertTrue(delete_dry_run["dry_run"])
        refresh_mock.assert_not_awaited()

    async def test_access_status_reports_nutrition_scopes_without_exposing_tokens(self) -> None:
        mcp = build_mcp()
        tool = mcp._tool_manager.get_tool("google_health.access_status")
        assert tool is not None
        with (
            patch(
                "heisenberg_access_mcp.server.refresh_google_access_token",
                new=AsyncMock(return_value=("secret-access-token", False)),
            ),
            patch(
                "heisenberg_access_mcp.server.fetch_google_health_access_status",
                new=AsyncMock(return_value={"ok": True, "endpoint": "google_health.identity"}),
            ),
        ):
            result = await tool.fn(None)

        self.assertIn(GOOGLE_HEALTH_NUTRITION_READ_SCOPE, result["required_scopes"])
        self.assertIn(GOOGLE_HEALTH_NUTRITION_WRITE_SCOPE, result["required_scopes"])
        self.assertNotIn("secret-access-token", json.dumps(result))

    async def test_all_five_nutrition_tools_are_registered(self) -> None:
        tools = {tool.name for tool in await build_mcp().list_tools()}
        self.assertTrue(
            {
                "google_health.log_meal",
                "google_health.get_nutrition_day",
                "google_health.get_nutrition_range",
                "google_health.correct_nutrition_item",
                "google_health.delete_nutrition_items",
            }.issubset(tools)
        )

    async def test_multi_item_create_reports_partial_failure_without_rollback(self) -> None:
        meal = normalize_google_health_meal(
            "2026-03-29T08:00:00+02:00",
            "BREAKFAST",
            [nutrition_item(display_name="Skyr, 250 g"), nutrition_item(display_name="Banane, 1 Stück")],
        )
        with patch(
            "heisenberg_access_mcp.server.google_health_create_nutrition_data_point",
            new=AsyncMock(
                side_effect=[
                    {"name": "operations/create-1", "pending": True},
                    CapabilityError("google_health_nutrition_create_failed", http_status=403),
                ]
            ),
        ):
            result = await create_google_health_meal("access-token", meal)

        self.assertFalse(result["ok"])
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(result["results"]), 2)
        self.assertRegex(result["results"][0]["requested_data_point_id"], GOOGLE_HEALTH_NUTRITION_DATA_POINT_ID_RE)
        self.assertNotIn("data_point_id", result["results"][0])
        self.assertEqual(result["results"][1]["error"], "google_health_nutrition_create_failed")

    async def test_partial_correction_reads_deletes_and_recreates_with_new_id(self) -> None:
        original_id = "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        create_mock = AsyncMock(
            return_value={
                "name": "operations/create-2",
                "done": True,
                "pending": False,
                "data_point_id": "2678973737190640032",
            }
        )
        with (
            patch(
                "heisenberg_access_mcp.server.google_health_get_nutrition_data_point",
                new=AsyncMock(return_value=nutrition_data_point(data_point_id=original_id)),
            ),
            patch(
                "heisenberg_access_mcp.server.google_health_batch_delete_nutrition_data_points",
                new=AsyncMock(return_value={"name": "operations/delete-1", "pending": True}),
            ) as delete_mock,
            patch(
                "heisenberg_access_mcp.server.google_health_create_nutrition_data_point",
                new=create_mock,
            ),
        ):
            result = await correct_google_health_nutrition_item(
                "access-token",
                data_point_id=original_id,
                changes=normalize_google_health_nutrition_correction_changes({"energy_kcal": 175}),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["new_data_point_id"], "2678973737190640032")
        self.assertNotEqual(result["requested_replacement_data_point_id"], original_id)
        delete_mock.assert_awaited_once_with("access-token", [original_id])
        replacement = create_mock.await_args.args[1]
        self.assertEqual(replacement["nutritionLog"]["energy"]["kcal"], 175.0)
        self.assertEqual(replacement["nutritionLog"]["nutrients"][0]["quantity"]["grams"], 27.5)
        self.assertFalse(result["rollback_attempted"])

    async def test_correction_rejects_identified_food_before_delete(self) -> None:
        original_id = "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        identified = nutrition_data_point(data_point_id=original_id)
        identified["nutritionLog"]["food"] = "users/me/dataTypes/food/dataPoints/catalog-food"
        delete_mock = AsyncMock()
        create_mock = AsyncMock()
        with (
            patch(
                "heisenberg_access_mcp.server.google_health_get_nutrition_data_point",
                new=AsyncMock(return_value=identified),
            ),
            patch(
                "heisenberg_access_mcp.server.google_health_batch_delete_nutrition_data_points",
                new=delete_mock,
            ),
            patch(
                "heisenberg_access_mcp.server.google_health_create_nutrition_data_point",
                new=create_mock,
            ),
        ):
            with self.assertRaisesRegex(CapabilityError, "nutrition_correction_requires_anonymous_item"):
                await correct_google_health_nutrition_item(
                    "access-token",
                    data_point_id=original_id,
                    changes={"energy_kcal": 175},
                )

        delete_mock.assert_not_awaited()
        create_mock.assert_not_awaited()

    async def test_correction_reports_deleted_original_when_recreate_fails(self) -> None:
        original_id = "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        with (
            patch(
                "heisenberg_access_mcp.server.google_health_get_nutrition_data_point",
                new=AsyncMock(return_value=nutrition_data_point(data_point_id=original_id)),
            ),
            patch(
                "heisenberg_access_mcp.server.google_health_batch_delete_nutrition_data_points",
                new=AsyncMock(return_value={"name": "operations/delete-1", "pending": True}),
            ),
            patch(
                "heisenberg_access_mcp.server.google_health_create_nutrition_data_point",
                new=AsyncMock(side_effect=CapabilityError("google_health_nutrition_create_failed", http_status=400)),
            ),
        ):
            result = await correct_google_health_nutrition_item(
                "access-token",
                data_point_id=original_id,
                changes={"fat_g": 1.0},
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["delete_accepted"])
        self.assertFalse(result["replacement_accepted"])
        self.assertFalse(result["rollback_attempted"])
        self.assertEqual(result["create_error"]["error"], "google_health_nutrition_create_failed")

    def test_delete_ids_are_concrete_deduplicated_and_bounded(self) -> None:
        ids = normalize_google_health_nutrition_data_point_ids(
            [
                "meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "users/123/dataTypes/nutrition-log/dataPoints/meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ]
        )
        self.assertEqual(ids, ["meal-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
        with self.assertRaisesRegex(CapabilityError, "nutrition_data_point_id_invalid"):
            normalize_google_health_nutrition_data_point_ids(["not/a/concrete/id"])

    def test_status_documents_required_nutrition_scopes(self) -> None:
        payload = google_health_activity_data_types_payload()
        self.assertEqual(
            payload["required_nutrition_scopes"],
            [GOOGLE_HEALTH_NUTRITION_READ_SCOPE, GOOGLE_HEALTH_NUTRITION_WRITE_SCOPE],
        )
        self.assertEqual(payload["nutrition_log"]["data_type"], "nutrition-log")


if __name__ == "__main__":
    unittest.main()
