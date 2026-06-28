from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route


LOGGER = logging.getLogger("heisenberg_access_mcp")
OPENBAO_HEALTH_STATUS_CODES = {200, 429, 472, 473, 501, 503}
OPENBAO_SECRET_MOUNT = "secret"
OPENBAO_ALLOWED_SECRETS = {
    "homeassistant": "heisenberg/homeassistant",
    "freshrss": "heisenberg/freshrss",
    "tandoor": "heisenberg/tandoor",
    "elevenlabs": "heisenberg/elevenlabs",
    "google_health_oauth_client": "heisenberg/google-health/oauth-client",
    "google_health_oauth_token": "heisenberg/google-health/oauth-token",
    "x_oauth": "heisenberg/x/oauth",
}
OPENBAO_WRITABLE_SECRETS = {"google_health_oauth_token", "x_oauth"}
DEFAULT_ARTIFACT_DIR = "/var/lib/heisenberg-access-mcp/artifacts"
MAX_ARTIFACT_DOWNLOAD_BYTES = 100 * 1024 * 1024
DEFAULT_INLINE_RESPONSE_BYTES = 256 * 1024
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SERVICE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
BINARY_ARTIFACT_CONTENT_TYPES = {
    "application/gzip",
    "application/pdf",
    "application/x-gzip",
    "application/zip",
}
TEXTUAL_ARTIFACT_CONTENT_TYPES = {
    "application/atom+xml",
    "application/javascript",
    "application/rss+xml",
    "application/x-www-form-urlencoded",
    "application/xhtml+xml",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "image/svg+xml",
}
TEXTUAL_ARTIFACT_SUFFIXES = ("+xml", "+yaml")
BINARY_ARTIFACT_PREFIXES = ("audio/", "image/", "video/")
SAFE_OPENBAO_FIELDS = (
    "initialized",
    "sealed",
    "standby",
    "performance_standby",
    "replication_perf_mode",
    "replication_dr_mode",
    "server_time_utc",
    "version",
    "storage_type",
)
X_TWEET_ID_RE = re.compile(r"(?<!\d)(\d{5,25})(?!\d)")
X_USER_ID_RE = re.compile(r"^\d{1,25}$")
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ELEVENLABS_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GOOGLE_HEALTH_CIVIL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?$")
GOOGLE_HEALTH_EXERCISE_DATA_POINT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TOKEN_REFRESH_SKEW = timedelta(minutes=5)
X_BOOKMARK_DEFAULT_PAGE_SIZE = 25
X_BOOKMARK_MAX_PAGE_SIZE = 100
X_UNBOOKMARK_MAX_TWEETS = 100
GOOGLE_HEALTH_ACTIVITY_SCOPE = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
GOOGLE_HEALTH_SLEEP_SCOPE = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
GOOGLE_HEALTH_HEALTH_METRICS_SCOPE = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
GOOGLE_HEALTH_LOCATION_SCOPE = "https://www.googleapis.com/auth/googlehealth.location.readonly"
GOOGLE_HEALTH_REQUIRED_READONLY_SCOPES = (
    GOOGLE_HEALTH_ACTIVITY_SCOPE,
    GOOGLE_HEALTH_SLEEP_SCOPE,
    GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
    GOOGLE_HEALTH_LOCATION_SCOPE,
)
GOOGLE_HEALTH_OPTIONAL_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
    "https://www.googleapis.com/auth/googlehealth.ecg.readonly",
    "https://www.googleapis.com/auth/googlehealth.irn.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.settings.readonly",
)
# Google Health path names stay hyphenated, but list filters for hyphenated
# data types are accepted with snake_case prefixes in live API probes.
GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES = {
    "active-energy-burned": {
        "field": "activeEnergyBurned",
        "filter_prefix": "active_energy_burned",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active calories burned activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "active-minutes": {
        "field": "activeMinutes",
        "filter_prefix": "active_minutes",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active minutes by activity level over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "active-zone-minutes": {
        "field": "activeZoneMinutes",
        "filter_prefix": "active_zone_minutes",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active zone minutes in heart-rate zones over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "altitude": {
        "field": "altitude",
        "filter_prefix": "altitude",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Altitude gain activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "distance": {
        "field": "distance",
        "filter_prefix": "distance",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Distance activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "exercise": {
        "field": "exercise",
        "filter_prefix": "exercise",
        "filter_kind": "session_civil_start",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Exercise/workout sessions with activity type, duration, and metrics summary.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
        "max_page_size": 25,
    },
    "heart-rate": {
        "field": "heartRate",
        "filter_prefix": "heart_rate",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Heart rate samples for activity, workout, and recovery context.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "run-vo2-max": {
        "field": "runVo2Max",
        "filter_prefix": "run_vo2_max",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Run VO2 max samples for fitness and workout performance context.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "steps": {
        "field": "steps",
        "filter_prefix": "steps",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Step count activity datapoints over an interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "time-in-heart-rate-zone": {
        "field": "timeInHeartRateZone",
        "filter_prefix": "time_in_heart_rate_zone",
        "filter_kind": "interval",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Time spent in heart-rate zones over an activity interval.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "vo2-max": {
        "field": "vo2Max",
        "filter_prefix": "vo2_max",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "VO2 max samples for cardio fitness and recovery context.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
}
GOOGLE_HEALTH_SLEEP_DATA_TYPES = {
    "sleep": {
        "field": "sleep",
        "filter_prefix": "sleep",
        "filter_kind": "sleep_session_civil_end",
        "scope": GOOGLE_HEALTH_SLEEP_SCOPE,
        "description": "Sleep sessions with interval, summary, sleep stages, and out-of-bed segments.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
        "max_page_size": 25,
    },
}
GOOGLE_HEALTH_HEALTH_METRIC_DATA_TYPES = {
    "blood-glucose": {
        "field": "bloodGlucose",
        "filter_prefix": "blood_glucose",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Blood glucose samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "body-fat": {
        "field": "bodyFat",
        "filter_prefix": "body_fat",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Body fat percentage samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "core-body-temperature": {
        "field": "coreBodyTemperature",
        "filter_prefix": "core_body_temperature",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Core body temperature samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "daily-heart-rate-variability": {
        "field": "dailyHeartRateVariability",
        "filter_prefix": "daily_heart_rate_variability",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily HRV and recovery metrics, including RMSSD when available.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "daily-heart-rate-zones": {
        "field": "dailyHeartRateZones",
        "filter_prefix": "daily_heart_rate_zones",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily heart-rate zone records.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "daily-oxygen-saturation": {
        "field": "dailyOxygenSaturation",
        "filter_prefix": "daily_oxygen_saturation",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily sleep oxygen saturation / SpO2 summary records.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "daily-respiratory-rate": {
        "field": "dailyRespiratoryRate",
        "filter_prefix": "daily_respiratory_rate",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily respiratory rate records.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "daily-resting-heart-rate": {
        "field": "dailyRestingHeartRate",
        "filter_prefix": "daily_resting_heart_rate",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily resting heart rate records for recovery context.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "daily-sleep-temperature-derivations": {
        "field": "dailySleepTemperatureDerivations",
        "filter_prefix": "daily_sleep_temperature_derivations",
        "filter_kind": "daily",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily sleep temperature derivation records.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "heart-rate": {
        "field": "heartRate",
        "filter_prefix": "heart_rate",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Heart rate samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
    "heart-rate-variability": {
        "field": "heartRateVariability",
        "filter_prefix": "heart_rate_variability",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "HRV samples, including RMSSD and standard deviation when available.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "height": {
        "field": "height",
        "filter_prefix": "height",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Height samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "oxygen-saturation": {
        "field": "oxygenSaturation",
        "filter_prefix": "oxygen_saturation",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Oxygen saturation / SpO2 samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "respiratory-rate-sleep-summary": {
        "field": "respiratoryRateSleepSummary",
        "filter_prefix": "respiratory_rate_sleep_summary",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Respiratory rate sleep summary samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": False,
    },
    "weight": {
        "field": "weight",
        "filter_prefix": "weight",
        "filter_kind": "sample",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Weight samples.",
        "supports_raw_points": True,
        "supports_daily_rollup": True,
    },
}
GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES = {
    "active-energy-burned": {
        "field": "activeEnergyBurned",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active calories burned, excluding basal calories.",
    },
    "active-minutes": {
        "field": "activeMinutes",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active minutes by activity level.",
    },
    "active-zone-minutes": {
        "field": "activeZoneMinutes",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Active zone minutes in heart-rate zones.",
    },
    "altitude": {
        "field": "altitude",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily altitude gain rollup.",
    },
    "blood-glucose": {
        "field": "bloodGlucose",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily blood glucose average rollup.",
    },
    "body-fat": {
        "field": "bodyFat",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily body fat average rollup.",
    },
    "core-body-temperature": {
        "field": "coreBodyTemperature",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily core body temperature min/max/average rollup.",
    },
    "distance": {
        "field": "distance",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily distance rollup.",
    },
    "floors": {
        "field": "floors",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily floors climbed rollup.",
    },
    "heart-rate": {
        "field": "heartRate",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily heart-rate min/max/average rollup.",
    },
    "run-vo2-max": {
        "field": "runVo2Max",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily run VO2 max min/max/average rollup.",
    },
    "sedentary-period": {
        "field": "sedentaryPeriod",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily sedentary-period rollup.",
    },
    "steps": {
        "field": "steps",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily step count rollup.",
    },
    "swim-lengths-data": {
        "field": "swimLengthsData",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily swim lengths rollup.",
    },
    "time-in-heart-rate-zone": {
        "field": "timeInHeartRateZone",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Daily time in heart-rate zones rollup.",
    },
    "total-calories": {
        "field": "totalCalories",
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "description": "Total calories burned, including basal calories.",
    },
    "weight": {
        "field": "weight",
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "description": "Daily weight average rollup.",
    },
}
GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES = {
    name: spec for name, spec in GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES.items()
    if spec["scope"] == GOOGLE_HEALTH_ACTIVITY_SCOPE
}
LEGACY_ELEVENLABS_OPERATIONS: dict[str, dict[str, Any]] = {
    "voices": {
        "method": "GET",
        "path": "/v2/voices",
        "description": "List available voices and voice metadata.",
    },
    "models": {
        "method": "GET",
        "path": "/v1/models",
        "description": "List available ElevenLabs models.",
    },
    "user_subscription": {
        "method": "GET",
        "path": "/v1/user/subscription",
        "description": "Read subscription/quota metadata without exposing the API key.",
    },
}
SENSITIVE_RESPONSE_KEYS = {
    "access_token",
    "accessToken",
    "api_key",
    "apiKey",
    "auth_token",
    "authToken",
    "authorization",
    "client_secret",
    "clientSecret",
    "id_token",
    "idToken",
    "password",
    "refresh_token",
    "refreshToken",
    "secret",
    "token",
    "x-api-key",
    "x_api_key",
    "xi-api-key",
    "xi_api_key",
}
NORMALIZED_SENSITIVE_RESPONSE_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in SENSITIVE_RESPONSE_KEYS
}
STRUCTURAL_SECRET_NAME_KEYS = {"header", "headerkey", "headername", "key", "name"}
STRUCTURAL_SECRET_VALUE_KEYS = {"headervalue", "value", "values"}

CAPABILITIES: dict[str, dict[str, Any]] = {
    "access.status": {
        "tool": "access_status",
        "enabled": True,
        "secret_access": False,
    },
    "openbao.status": {
        "tool": "openbao_status",
        "enabled": True,
        "secret_access": False,
    },
    "x.get_tweet": {
        "tool": "x.get_tweet",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth token",
        "writes": "refreshes OAuth tokens back to OpenBao when needed",
    },
    "x.list_bookmarks": {
        "tool": "x.list_bookmarks",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth token",
        "scope": "read current X bookmarks for Tim through the official X API, with pagination and tweet/media/author context for Brain ingest",
        "writes": "refreshes OAuth tokens and may cache X user_id metadata in OpenBao when needed",
    },
    "x.unbookmark_tweets": {
        "tool": "x.unbookmark_tweets",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth token",
        "scope": "remove one or more X bookmarks after successful ingest; supports dry_run",
        "writes": "calls X bookmark delete endpoints only with confirm=true; also refreshes OAuth tokens when needed",
    },
    "google_health.access_status": {
        "tool": "google_health.access_status",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.list_data_types": {
        "tool": "google_health.list_data_types",
        "enabled": True,
        "secret_access": False,
        "scope": "documents Google Health fitness data, exercise, workout, activity, sleep, health metrics, heart rate, HRV, recovery, oxygen saturation, respiratory rate, weight, body fat, route, location, TCX, and required readonly OAuth scopes",
    },
    "google_health.get_activity_data_points": {
        "tool": "google_health.get_activity_data_points",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health activity data points for allowlisted data types such as steps, distance, calories, active minutes, heart-rate zones, altitude, VO2 max, and workout-adjacent metrics; paginated",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.get_exercise_data_points": {
        "tool": "google_health.get_exercise_data_points",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health exercise/workout data points for a date range; paginated with page_size <= 25",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.export_exercise_tcx": {
        "tool": "google_health.export_exercise_tcx",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health workout route/location TCX export by exercise data_point_id; stores XML as private artifact metadata only",
        "writes": "updates token metadata only if Google returns replacement token metadata; stores private runtime artifact",
    },
    "google_health.get_sleep_data_points": {
        "tool": "google_health.get_sleep_data_points",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health sleep sessions, sleep stages, sleep summary, daily log, recovery, and health datapoints for a date range; paginated with page_size <= 25",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.summarize_activity_day": {
        "tool": "google_health.summarize_activity_day",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only daily log activity summary for steps, calories, distance, active minutes, heart rate, heart-rate zones, altitude, floors, VO2 max, and workouts",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.summarize_sleep_day": {
        "tool": "google_health.summarize_sleep_day",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only daily sleep summary for sleep sessions, sleep stages, sleep duration, recovery, daily log, and health datapoints",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.get_health_metric_data_points": {
        "tool": "google_health.get_health_metric_data_points",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only Google Health metrics for allowlisted data types such as heart rate, resting HR, HRV, recovery, oxygen saturation/SpO2, respiratory rate, weight, body fat, temperature, blood glucose, and daily health datapoints; paginated",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "google_health.summarize_health_day": {
        "tool": "google_health.summarize_health_day",
        "enabled": True,
        "secret_access": "server-side OpenBao OAuth client and refresh token",
        "scope": "read-only daily health metrics summary for heart rate, resting HR, HRV, recovery, oxygen saturation/SpO2, respiratory rate, weight, body fat, temperature, and other allowlisted health datapoints",
        "writes": "updates token metadata only if Google returns replacement token metadata",
    },
    "elevenlabs.request": {
        "tool": "elevenlabs.request",
        "enabled": True,
        "secret_access": "server-side OpenBao API key",
        "scope": "https://api.elevenlabs.io only; binary/large responses become artifacts",
    },
    "elevenlabs.text_to_speech": {
        "tool": "elevenlabs.text_to_speech",
        "enabled": True,
        "secret_access": "server-side OpenBao API key",
        "returns": "artifact metadata only; audio is stored server-side",
    },
    "homeassistant.request": {
        "tool": "homeassistant.request",
        "enabled": True,
        "secret_access": "server-side OpenBao Home Assistant token",
        "scope": "configured Home Assistant base URL only",
    },
    "freshrss.request": {
        "tool": "freshrss.request",
        "enabled": True,
        "secret_access": "server-side OpenBao FreshRSS API password",
        "scope": "configured FreshRSS API base URL only",
    },
    "tandoor.request": {
        "tool": "tandoor.request",
        "enabled": True,
        "secret_access": "server-side OpenBao Tandoor API key",
        "scope": "configured Tandoor base URL only",
    },
}


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")


def emit_event(event: str, **payload: Any) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **payload,
    }
    LOGGER.info(json.dumps(record, sort_keys=True))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def bool_from_health(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    return value if isinstance(value, bool) else None


def redact_openbao_status(data: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for field in SAFE_OPENBAO_FIELDS:
        if field in data:
            safe[field] = data[field]
    return safe


def openbao_target_summary(addr: str) -> dict[str, str | None]:
    parsed = urlparse(addr)
    return {
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": str(parsed.port) if parsed.port else None,
    }


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def artifact_dir() -> Path:
    return Path(os.environ.get("HEISENBERG_ACCESS_MCP_ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR))


def artifact_base_url(resource_url: str) -> str | None:
    explicit = os.environ.get("HEISENBERG_ACCESS_MCP_ARTIFACT_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    parsed = urlparse(resource_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def artifact_download_url(resource_url: str, artifact_id: str) -> str | None:
    base_url = artifact_base_url(resource_url)
    if not base_url:
        return None
    return f"{base_url}/artifacts/{artifact_id}"


def extension_for_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", maxsplit=1)[0].strip().lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
        "application/vnd.garmin.tcx+xml": ".tcx",
        "application/xml": ".xml",
        "text/xml": ".xml",
    }.get(normalized, ".bin")


def inline_response_limit() -> int:
    raw = os.environ.get("HEISENBERG_ACCESS_MCP_INLINE_RESPONSE_BYTES", "").strip()
    if not raw:
        return DEFAULT_INLINE_RESPONSE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_RESPONSE_BYTES
    return max(1024, value)


def metadata_path_for(artifact_id: str) -> Path:
    return artifact_dir() / f"{artifact_id}.json"


def artifact_path_for(artifact_id: str, mime_type: str) -> Path:
    return artifact_dir() / f"{artifact_id}{extension_for_mime_type(mime_type)}"


def read_artifact_metadata(artifact_id: str) -> dict[str, Any] | None:
    if not ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None

    path = metadata_path_for(artifact_id)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def store_artifact(content: bytes, mime_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    artifact_dir().mkdir(parents=True, exist_ok=True)
    artifact_id = secrets.token_urlsafe(24)
    sha256 = hashlib.sha256(content).hexdigest()
    path = artifact_path_for(artifact_id, mime_type)
    path.write_bytes(content)

    payload = {
        "artifact_id": artifact_id,
        "mime_type": mime_type,
        "byte_size": len(content),
        "sha256": sha256,
        "created_at": iso_now(),
        "filename": path.name,
        **metadata,
    }
    metadata_path_for(artifact_id).write_text(json.dumps(payload, sort_keys=True))
    return payload


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.isdigit():
        return parse_datetime(int(text))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def expires_soon(expires_at: Any) -> bool:
    parsed = parse_datetime(expires_at)
    return parsed is None or parsed <= utc_now() + TOKEN_REFRESH_SKEW


def required_secret_value(secret: dict[str, Any], key: str) -> str:
    value = secret.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("secret_missing_required_key", key=key)
    return value.strip()


def normalized_response_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_response_key(key: str) -> bool:
    normalized_key = normalized_response_key(key)
    sensitive_fragments = (
        "authorization",
        "apikey",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "cookie",
        "idtoken",
        "personalaccesstoken",
        "password",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessiontoken",
        "token",
        "xapikey",
        "xiapikey",
    )
    return (
        key in SENSITIVE_RESPONSE_KEYS
        or normalized_key in NORMALIZED_SENSITIVE_RESPONSE_KEYS
        or any(fragment in normalized_key for fragment in sensitive_fragments)
    )


def is_sensitive_structural_name(value: str) -> bool:
    text = value.strip().strip("'\"")
    if any(separator in text for separator in (":", "=", " ", "\t", "\r", "\n")):
        return False
    return is_sensitive_response_key(text)


def is_probable_standalone_token(value: str) -> bool:
    text = value.strip().strip("'\"")
    if not text or any(character.isspace() for character in text):
        return False
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", text):
        return True
    if re.fullmatch(r"ya29\.[A-Za-z0-9._-]{20,}", text):
        return True
    if len(text) >= 40 and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", text):
        has_letter = bool(re.search(r"[A-Za-z]", text))
        has_digit = bool(re.search(r"\d", text))
        return has_letter and has_digit
    return False


def redacted_array_item(value: Any) -> Any:
    if isinstance(value, str):
        redacted = redacted_text(value)
        if redacted != value:
            return redacted
        if is_probable_standalone_token(value):
            return "[redacted]"
    return redacted_json(value)


def has_sensitive_structural_name(value: dict[Any, Any]) -> bool:
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        if normalized_response_key(key) in STRUCTURAL_SECRET_NAME_KEYS and is_sensitive_structural_name(item):
            return True
    return False


def redacted_json(value: Any) -> Any:
    if isinstance(value, dict):
        redact_structural_values = has_sensitive_structural_name(value)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = normalized_response_key(key)
            if is_sensitive_response_key(key):
                redacted[key] = "[redacted]"
            elif redact_structural_values and normalized_key in STRUCTURAL_SECRET_VALUE_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redacted_json(item)
        return redacted

    if isinstance(value, list):
        if len(value) >= 2 and isinstance(value[0], str) and is_sensitive_structural_name(value[0]):
            return [redacted_json(value[0]), *(["[redacted]"] * (len(value) - 1))]
        return [redacted_array_item(item) for item in value]
    if isinstance(value, str):
        return redacted_text(value)

    return value


def redacted_text(value: str) -> str:
    patterns = (
        r"(?im)^(Auth=).+$",
        r"(?i)(Authorization['\"]?\s*[:=]\s*['\"]?)[^'\",\r\n}\]]+",
        r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+",
        r"(?i)(Cookie\s*:\s*)[^\r\n]+",
        r"(?i)((?:x-api-key|xi-api-key)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}\]\r\n]+",
        r"(?i)((?:[A-Za-z0-9-]*Auth-Token|[A-Za-z0-9-]*Access-Token|[A-Za-z0-9-]*Api-Key|[A-Za-z0-9-]*ApiKey)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}\]\r\n]+",
        r"(?i)\b(Bearer\s+)[^'\",\s}\]\r\n]+",
        r"(?i)\b(Basic\s+)[A-Za-z0-9+/=._-]+",
        r"(?i)\b(Token\s+)[^'\",\s}\]\r\n]+",
        r"(?i)\b(GoogleLogin\s+auth=)[^&'\",\s}\]\r\n]+",
        r"(?i)^(sk_(?:live|test)?_?)[A-Za-z0-9_-]+",
        r"(?i)(auth=)[^&\s]+",
        r"(?i)((?<![A-Za-z0-9_-])(?:access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|token)['\"]?\s*[:=]\s*['\"]?)[^&'\",\s}\]\r\n]+",
    )
    redacted = value
    for pattern in patterns:
        redacted = re.sub(pattern, r"\1[redacted]", redacted)
    return redacted


def safe_response_headers_from_headers(source_headers: httpx.Headers) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key in ("content-type", "content-length", "etag", "last-modified"):
        value = source_headers.get(key)
        if value:
            headers[key] = value
    return headers


def safe_response_headers(response: httpx.Response) -> dict[str, str]:
    return safe_response_headers_from_headers(response.headers)


def headers_for_decoded_response(source_headers: httpx.Headers) -> httpx.Headers:
    headers = httpx.Headers(source_headers)
    for header in ("content-encoding", "content-length"):
        if header in headers:
            del headers[header]
    return headers


def normalize_service_method(method: str) -> str:
    normalized = method.upper().strip()
    if normalized not in SERVICE_METHODS:
        raise CapabilityError("method_not_supported", allowed_methods=sorted(SERVICE_METHODS))
    return normalized


def normalize_response_mode(response_mode: str) -> str:
    mode = response_mode.strip().lower()
    if mode not in {"auto", "inline", "artifact"}:
        raise CapabilityError("invalid_response_mode", allowed_modes=["auto", "inline", "artifact"])
    return mode


def response_read_limit_for_mode(mode: str) -> int:
    if mode == "inline":
        return inline_response_limit()
    return MAX_ARTIFACT_DOWNLOAD_BYTES


def validate_service_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise CapabilityError("path_required")

    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or path.startswith("//"):
        raise CapabilityError("absolute_urls_not_allowed")
    if "?" in path:
        raise CapabilityError("query_string_not_allowed_use_params")

    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        decoded_segment = unquote(segment)
        if decoded_segment in {".", ".."} or "/" in decoded_segment or "\\" in decoded_segment:
            raise CapabilityError("path_traversal_not_allowed")

    normalized_path = "/" + "/".join(segments)
    if segments and path.endswith("/"):
        return f"{normalized_path}/"
    return normalized_path


def join_service_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CapabilityError("service_base_url_invalid")

    normalized_path = validate_service_path(path)
    return f"{base_url.rstrip('/')}{normalized_path}"


def normalize_mapping(value: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CapabilityError(f"{name}_must_be_object")
    return value


def ensure_write_confirmed(method: str, confirm: bool) -> None:
    if method in MUTATING_METHODS and not confirm:
        raise CapabilityError(
            "confirmation_required_for_mutating_method",
            method=method,
            message="Mutating service requests require explicit current user approval and confirm=true.",
        )


def dry_run_payload(method: str, url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    return {
        "ok": True,
        "dry_run": True,
        "method": method,
        "target": {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "path": parsed.path,
        },
    }


def sanitized_api_error(response: httpx.Response) -> dict[str, Any]:
    payload: Any
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = redacted_text(response.text[:500])
    return {
        "http_status": response.status_code,
        "body": redacted_json(payload),
    }


def is_inline_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return is_json_content_type(content_type) or normalized.startswith("text/")


def inline_response_payload(response: httpx.Response, content_type: str) -> dict[str, Any]:
    if is_json_content_type(content_type):
        try:
            return {"response": redacted_json(response.json())}
        except json.JSONDecodeError:
            raise CapabilityError(
                "json_response_parse_failed",
                message="Malformed JSON responses are refused because text redaction is not reliable enough.",
            ) from None

    return {"response_text": redacted_text(response.text)}


def is_json_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return normalized == "application/json" or normalized.endswith("+json")


def is_text_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return (
        normalized.startswith("text/")
        or normalized in TEXTUAL_ARTIFACT_CONTENT_TYPES
        or normalized.endswith(TEXTUAL_ARTIFACT_SUFFIXES)
    )


def is_binary_artifact_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", maxsplit=1)[0].strip().lower()
    return normalized in BINARY_ARTIFACT_CONTENT_TYPES or normalized.startswith(BINARY_ARTIFACT_PREFIXES)


def artifact_content_for_response(response: httpx.Response, content_type: str) -> tuple[bytes, str]:
    if is_json_content_type(content_type):
        try:
            payload = redacted_json(response.json())
        except json.JSONDecodeError:
            raise CapabilityError(
                "json_artifact_parse_failed",
                message="Malformed JSON responses are refused as artifacts because text redaction is not reliable enough.",
            ) from None
        return json.dumps(payload, sort_keys=True).encode(), "application/json"

    if is_text_content_type(content_type):
        raise CapabilityError(
            "text_artifact_refused",
            message="Text responses are not stored as artifacts because secret redaction is not reliable enough.",
        )

    if not is_binary_artifact_content_type(content_type):
        raise CapabilityError(
            "artifact_content_type_not_allowed",
            content_type=content_type,
            message="Only known binary content types and redacted JSON may be stored as artifacts.",
        )

    return response.content, content_type


def service_response_payload(
    response: httpx.Response,
    *,
    response_mode: str,
    resource_url: str,
    artifact_metadata: dict[str, Any],
) -> dict[str, Any]:
    mode = normalize_response_mode(response_mode)

    content_type = response.headers.get("content-type", "application/octet-stream")
    byte_size = len(response.content)
    can_inline = is_inline_content_type(content_type) and byte_size <= inline_response_limit()

    base = {
        "ok": response.status_code < 400,
        "http_status": response.status_code,
        "content_type": content_type,
        "byte_size": byte_size,
        "headers": safe_response_headers(response),
    }

    if byte_size == 0:
        return {**base, "response_empty": True}

    if mode == "inline" and not can_inline:
        return {
            **base,
            "ok": False,
            "error": "response_too_large_or_binary_for_inline",
            "max_inline_bytes": inline_response_limit(),
        }

    if mode == "artifact" or (mode == "auto" and not can_inline):
        if byte_size > MAX_ARTIFACT_DOWNLOAD_BYTES:
            return {
                **base,
                "ok": False,
                "error": "response_too_large_for_artifact",
                "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
            }

        artifact_content, artifact_mime_type = artifact_content_for_response(response, content_type)
        if len(artifact_content) > MAX_ARTIFACT_DOWNLOAD_BYTES:
            return {
                **base,
                "ok": False,
                "error": "response_too_large_for_artifact",
                "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
                "artifact_byte_size": len(artifact_content),
            }

        artifact = store_artifact(
            artifact_content,
            artifact_mime_type,
            {
                **artifact_metadata,
                "provider_http_status": response.status_code,
                "provider_content_type": content_type,
            },
        )
        artifact["download"] = {
            "url": artifact_download_url(resource_url, artifact["artifact_id"]),
            "authorization": "Bearer token required; same private MCP bearer token",
            "exposure": "same private MCP HTTP service; Traefik remains disabled",
        }
        return {**base, "artifact": artifact}

    return {**base, **inline_response_payload(response, content_type)}


def oversized_response_payload(
    *,
    mode: str,
    status_code: int,
    content_type: str,
    byte_size: int,
    headers: httpx.Headers,
) -> dict[str, Any]:
    base = {
        "ok": False,
        "http_status": status_code,
        "content_type": content_type,
        "byte_size": byte_size,
        "headers": safe_response_headers_from_headers(headers),
    }
    if mode == "inline":
        return {
            **base,
            "error": "response_too_large_or_binary_for_inline",
            "max_inline_bytes": inline_response_limit(),
        }
    return {
        **base,
        "error": "response_too_large_for_artifact",
        "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
    }


async def limited_service_response(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    form_body: dict[str, Any] | None,
    mode: str,
) -> tuple[httpx.Response | None, dict[str, Any] | None]:
    limit = response_read_limit_for_mode(mode)
    chunks: list[bytes] = []
    byte_size = 0

    async with client.stream(
        method,
        url,
        headers=headers,
        params=params,
        json=json_body,
        data=form_body,
    ) as response:
        content_type = response.headers.get("content-type", "application/octet-stream")
        async for chunk in response.aiter_bytes():
            byte_size += len(chunk)
            if byte_size > limit:
                return None, oversized_response_payload(
                    mode=mode,
                    status_code=response.status_code,
                    content_type=content_type,
                    byte_size=byte_size,
                    headers=response.headers,
                )
            chunks.append(chunk)

        buffered = httpx.Response(
            response.status_code,
            headers=headers_for_decoded_response(response.headers),
            content=b"".join(chunks),
            request=response.request,
        )
        return buffered, None


class CapabilityError(Exception):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


class OpenBaoError(Exception):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class OpenBaoKV2:
    def __init__(self, addr: str, token: str | None) -> None:
        self._addr = addr.rstrip("/")
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise OpenBaoError("openbao_token_missing")
        return {
            "X-Vault-Request": "true",
            "X-Vault-Token": self._token,
        }

    def _path_for(self, secret_name: str) -> str:
        if secret_name not in OPENBAO_ALLOWED_SECRETS:
            raise OpenBaoError("openbao_secret_not_allowed")
        return OPENBAO_ALLOWED_SECRETS[secret_name]

    def _data_url_for(self, secret_name: str) -> str:
        secret_path = self._path_for(secret_name)
        return f"{self._addr}/v1/{OPENBAO_SECRET_MOUNT}/data/{secret_path}"

    async def read(self, secret_name: str) -> dict[str, Any]:
        url = self._data_url_for(secret_name)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=self._headers)

        if response.status_code != 200:
            raise OpenBaoError("openbao_read_failed", status_code=response.status_code)

        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise OpenBaoError("openbao_invalid_json") from error

        data = payload.get("data", {})
        secret = data.get("data", {})
        if not isinstance(secret, dict):
            raise OpenBaoError("openbao_invalid_secret_shape")
        return secret

    async def write(self, secret_name: str, secret: dict[str, Any]) -> None:
        if secret_name not in OPENBAO_WRITABLE_SECRETS:
            raise OpenBaoError("openbao_secret_not_writable")

        url = self._data_url_for(secret_name)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers=self._headers, json={"data": secret})

        if response.status_code not in {200, 204}:
            raise OpenBaoError("openbao_write_failed", status_code=response.status_code)


def transport_security_settings(resource_url: str) -> TransportSecuritySettings:
    parsed = urlparse(resource_url)
    allowed_hosts = {
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    }
    allowed_origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    }

    if parsed.netloc:
        allowed_hosts.add(parsed.netloc)
        allowed_origins.add(f"{parsed.scheme or 'http'}://{parsed.netloc}")

    allowed_hosts.update(comma_list(os.environ.get("HEISENBERG_ACCESS_MCP_ALLOWED_HOSTS", "")))
    allowed_origins.update(comma_list(os.environ.get("HEISENBERG_ACCESS_MCP_ALLOWED_ORIGINS", "")))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )


class StaticBearerTokenVerifier(TokenVerifier):
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._expected_token):
            return None

        return AccessToken(
            token="accepted-static-token",
            client_id="heisenberg-access-mcp-env-token",
            scopes=["mcp:call"],
        )


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = asyncio.get_running_loop().time()
        response = await call_next(request)
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        emit_event(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            client_host=request.client.host if request.client else None,
            duration_ms=duration_ms,
        )
        return response


async def read_openbao_health(openbao_addr: str, openbao_token: str | None) -> dict[str, Any]:
    headers = {"X-Vault-Request": "true"}
    if openbao_token:
        headers["X-Vault-Token"] = openbao_token

    url = f"{openbao_addr.rstrip('/')}/v1/sys/health?standbyok=true&perfstandbyok=true"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, headers=headers)

    reachable = response.status_code in OPENBAO_HEALTH_STATUS_CODES
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {}

    safe_body = redact_openbao_status(body if isinstance(body, dict) else {})
    initialized = bool_from_health(safe_body, "initialized")
    sealed = bool_from_health(safe_body, "sealed")

    return {
        "reachable": reachable,
        "ready": reachable and initialized is True and sealed is False,
        "http_status": response.status_code,
        **safe_body,
    }


def extract_tweet_id(tweet_id_or_url: str) -> str:
    match = X_TWEET_ID_RE.search(tweet_id_or_url)
    if not match:
        raise CapabilityError("invalid_tweet_id")
    return match.group(1)


def normalize_x_tweet_ids(tweet_ids_or_urls: Any) -> list[str]:
    if isinstance(tweet_ids_or_urls, str):
        values: list[Any] = [tweet_ids_or_urls]
    elif isinstance(tweet_ids_or_urls, list):
        values = tweet_ids_or_urls
    else:
        raise CapabilityError("tweet_ids_or_urls_must_be_string_or_list")

    tweet_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise CapabilityError("tweet_id_or_url_must_be_non_empty_string")
        tweet_id = extract_tweet_id(value)
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        tweet_ids.append(tweet_id)

    if not tweet_ids:
        raise CapabilityError("tweet_ids_required")
    if len(tweet_ids) > X_UNBOOKMARK_MAX_TWEETS:
        raise CapabilityError("too_many_tweet_ids", maximum=X_UNBOOKMARK_MAX_TWEETS, provided=len(tweet_ids))
    return tweet_ids


def normalize_x_pagination_token(pagination_token: str | None) -> str | None:
    if pagination_token is None:
        return None
    if not isinstance(pagination_token, str) or not pagination_token.strip():
        raise CapabilityError("pagination_token_must_be_non_empty_string")
    normalized = pagination_token.strip()
    if len(normalized) > 2048:
        raise CapabilityError("pagination_token_too_long", maximum=2048)
    return normalized


def normalize_x_user_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if not X_USER_ID_RE.fullmatch(normalized):
        raise CapabilityError("x_user_id_invalid")
    return normalized


def x_user_id_from_oauth(oauth: dict[str, Any]) -> str | None:
    for key in ("user_id", "x_user_id", "authenticated_user_id"):
        user_id = normalize_x_user_id(oauth.get(key))
        if user_id is not None:
            return user_id

    user = oauth.get("user")
    if isinstance(user, dict):
        user_id = normalize_x_user_id(user.get("id"))
        if user_id is not None:
            return user_id
    return None


async def refresh_x_access_token(openbao: OpenBaoKV2, oauth: dict[str, Any]) -> dict[str, Any]:
    client_id = required_secret_value(oauth, "client_id")
    refresh_token = required_secret_value(oauth, "refresh_token")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.x.com/2/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        raise CapabilityError("x_token_refresh_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_token_refresh_invalid_json") from error

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CapabilityError("x_token_refresh_missing_access_token")

    refreshed = dict(oauth)
    refreshed["access_token"] = access_token
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
        refreshed["refresh_token"] = payload["refresh_token"]
    if isinstance(payload.get("scope"), str) and payload["scope"]:
        refreshed["scope"] = payload["scope"]

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        refreshed["expires_at"] = (utc_now() + timedelta(seconds=int(expires_in))).isoformat()
    else:
        refreshed.pop("expires_at", None)

    refreshed["stored_at"] = iso_now()
    await openbao.write("x_oauth", refreshed)
    emit_event("oauth_token_refreshed", provider="x")
    return refreshed


async def fetch_x_authenticated_user(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://api.x.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user.fields": "id,name,username"},
        )

    if response.status_code != 200:
        raise CapabilityError("x_authenticated_user_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_authenticated_user_invalid_json") from error

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CapabilityError("x_authenticated_user_missing_data")
    if normalize_x_user_id(data.get("id")) is None:
        raise CapabilityError("x_authenticated_user_missing_id")
    return data


async def ensure_x_user_id(openbao: OpenBaoKV2, oauth: dict[str, Any]) -> tuple[dict[str, Any], str]:
    user_id = x_user_id_from_oauth(oauth)
    if user_id is not None:
        return oauth, user_id

    try:
        user = await fetch_x_authenticated_user(required_secret_value(oauth, "access_token"))
    except CapabilityError as error:
        if error.code != "x_authenticated_user_request_failed" or error.details.get("http_status") != 401:
            raise
        oauth = await refresh_x_access_token(openbao, oauth)
        user = await fetch_x_authenticated_user(required_secret_value(oauth, "access_token"))

    user_id = normalize_x_user_id(user.get("id"))
    if user_id is None:
        raise CapabilityError("x_authenticated_user_missing_id")

    updated = dict(oauth)
    updated["user_id"] = user_id
    updated["user_metadata_updated_at"] = iso_now()
    await openbao.write("x_oauth", updated)
    emit_event("x_user_id_cached")
    return updated, user_id


def extract_x_media_urls(media: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        for key in ("url", "preview_image_url"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in urls:
                urls.append(value)
        variants = item.get("variants")
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                value = variant.get("url")
                if isinstance(value, str) and value and value not in urls:
                    urls.append(value)
    return urls


def x_media_for_tweet(tweet: dict[str, Any], media: list[Any]) -> list[dict[str, Any]]:
    attachments = tweet.get("attachments")
    if not isinstance(attachments, dict):
        return []
    media_keys = attachments.get("media_keys")
    if not isinstance(media_keys, list):
        return []
    wanted = {key for key in media_keys if isinstance(key, str)}
    if not wanted:
        return []
    return [
        item
        for item in media
        if isinstance(item, dict) and isinstance(item.get("media_key"), str) and item["media_key"] in wanted
    ]


def x_tweet_payload(tweet: dict[str, Any], includes: dict[str, Any]) -> dict[str, Any]:
    users = includes.get("users", []) if isinstance(includes.get("users"), list) else []
    media = includes.get("media", []) if isinstance(includes.get("media"), list) else []
    author = next(
        (item for item in users if isinstance(item, dict) and item.get("id") == tweet.get("author_id")),
        None,
    )
    username = author.get("username") if isinstance(author, dict) and isinstance(author.get("username"), str) else None
    tweet_id = tweet.get("id")
    url = f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else f"https://x.com/i/web/status/{tweet_id}"

    return {
        "id": tweet_id,
        "text": tweet.get("text"),
        "author": author if isinstance(author, dict) else None,
        "url": url,
        "created_at": tweet.get("created_at"),
        "public_metrics": tweet.get("public_metrics", {}),
        "media_urls": extract_x_media_urls(x_media_for_tweet(tweet, media)),
    }


async def fetch_x_tweet(access_token: str, tweet_id: str) -> dict[str, Any]:
    params = {
        "tweet.fields": "attachments,author_id,created_at,entities,public_metrics",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "id,name,protected,username",
        "media.fields": "alt_text,media_key,preview_image_url,public_metrics,type,url,variants,width,height",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"https://api.x.com/2/tweets/{tweet_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("x_tweet_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_tweet_invalid_json") from error

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CapabilityError("x_tweet_missing_data")

    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = includes.get("users", []) if isinstance(includes.get("users"), list) else []
    author = next((item for item in users if isinstance(item, dict) and item.get("id") == data.get("author_id")), None)
    if not isinstance(author, dict) or author.get("protected") is not False:
        raise CapabilityError("x_tweet_public_status_unverified")
    return {"ok": True, **x_tweet_payload(data, includes)}


async def fetch_x_bookmarks(
    access_token: str,
    user_id: str,
    *,
    page_size: int,
    pagination_token: str | None,
) -> dict[str, Any]:
    params = {
        "max_results": str(page_size),
        "tweet.fields": "attachments,author_id,created_at,entities,public_metrics,referenced_tweets",
        "expansions": "author_id,attachments.media_keys",
        "user.fields": "id,name,protected,username",
        "media.fields": "alt_text,media_key,preview_image_url,public_metrics,type,url,variants,width,height",
    }
    if pagination_token is not None:
        params["pagination_token"] = pagination_token

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"https://api.x.com/2/users/{user_id}/bookmarks",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("x_bookmarks_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_bookmarks_invalid_json") from error

    data = payload.get("data")
    tweets = data if isinstance(data, list) else []
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    next_token = meta.get("next_token")

    return {
        "ok": True,
        "endpoint": "x.bookmarks.list",
        "page_size": page_size,
        "result_count": meta.get("result_count", len(tweets)),
        "next_token": next_token if isinstance(next_token, str) and next_token else None,
        "tweets": [x_tweet_payload(tweet, includes) for tweet in tweets if isinstance(tweet, dict)],
    }


async def delete_x_bookmark(access_token: str, user_id: str, tweet_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            f"https://api.x.com/2/users/{user_id}/bookmarks/{tweet_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if response.status_code not in {200, 204}:
        raise CapabilityError("x_unbookmark_request_failed", tweet_id=tweet_id, **sanitized_api_error(response))

    if response.status_code == 204 or not response.content:
        return {"ok": True, "tweet_id": tweet_id, "bookmarked": False}

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("x_unbookmark_invalid_json", tweet_id=tweet_id) from error

    data = payload.get("data") if isinstance(payload, dict) else None
    bookmarked = data.get("bookmarked") if isinstance(data, dict) else None
    return {
        "ok": True,
        "tweet_id": tweet_id,
        "bookmarked": bookmarked if isinstance(bookmarked, bool) else False,
    }


async def refresh_google_access_token(openbao: OpenBaoKV2) -> tuple[str, bool]:
    client_secret = await openbao.read("google_health_oauth_client")
    token_secret = await openbao.read("google_health_oauth_token")

    client_id = required_secret_value(client_secret, "client_id")
    client_secret_value = required_secret_value(client_secret, "client_secret")
    token_uri = required_secret_value(client_secret, "token_uri")
    refresh_token = required_secret_value(token_secret, "refresh_token")

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret_value,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        raise CapabilityError("google_token_refresh_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("google_token_refresh_invalid_json") from error

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CapabilityError("google_token_refresh_missing_access_token")

    wrote_metadata = False
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"] != refresh_token:
        updated = dict(token_secret)
        updated["refresh_token"] = payload["refresh_token"]
        updated["stored_at"] = iso_now()
        await openbao.write("google_health_oauth_token", updated)
        wrote_metadata = True
        emit_event("oauth_token_refreshed", provider="google_health")

    return access_token, wrote_metadata


async def fetch_google_health_access_status(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://health.googleapis.com/v4/users/me/identity",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if response.status_code != 200:
        return {
            "ok": False,
            "endpoint": "google_health.identity",
            "status": "health_api_endpoint_unavailable",
            "error": sanitized_api_error(response),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {
            "ok": False,
            "endpoint": "google_health.identity",
            "status": "health_api_invalid_json",
        }

    health_user_id = payload.get("healthUserId")
    legacy_user_id = payload.get("legacyUserId")

    return {
        "ok": True,
        "endpoint": "google_health.identity",
        "identity": {
            "healthUserId": health_user_id if isinstance(health_user_id, str) else None,
            "legacyUserId": legacy_user_id if isinstance(legacy_user_id, str) else None,
        },
    }


def parse_iso_date(value: str, name: str = "date") -> date:
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value.strip()):
        raise CapabilityError(f"{name}_must_be_iso_date", expected_format="YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise CapabilityError(f"{name}_invalid", expected_format="YYYY-MM-DD") from error


def google_health_default_start_date(days_back: int) -> str:
    return (utc_now().date() - timedelta(days=days_back)).isoformat()


def google_health_default_end_date() -> str:
    return (utc_now().date() + timedelta(days=1)).isoformat()


def normalize_google_health_civil_time(value: str | None, *, default: str, name: str) -> str:
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise CapabilityError(
            f"{name}_must_be_civil_time",
            expected_format="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS without timezone",
        )
    if not GOOGLE_HEALTH_CIVIL_TIME_RE.fullmatch(text):
        raise CapabilityError(
            f"{name}_must_be_civil_time",
            expected_format="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS without timezone",
        )
    return text


def normalize_page_size(value: int | None, *, default: int, minimum: int, maximum: int, name: str = "page_size") -> int:
    if value is None:
        return default
    if not isinstance(value, int):
        raise CapabilityError(f"{name}_must_be_integer")
    if value < minimum or value > maximum:
        raise CapabilityError(f"{name}_out_of_range", min=minimum, max=maximum)
    return value


def google_health_date_json(value: date) -> dict[str, int]:
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
    }


def google_health_data_type_payload(registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        name: {
            "field": spec["field"],
            "filter_kind": spec.get("filter_kind"),
            "required_scope": spec.get("scope"),
            "description": spec["description"],
            "supports_raw_points": spec.get("supports_raw_points", False),
            "supports_daily_rollup": spec.get("supports_daily_rollup", False),
            **({"max_page_size": spec["max_page_size"]} if "max_page_size" in spec else {}),
            **({"daily_rollup_field": spec["daily_rollup_field"]} if "daily_rollup_field" in spec else {}),
        }
        for name, spec in sorted(registry.items())
    }


def normalize_google_health_data_type(
    data_type: str,
    registry: dict[str, dict[str, Any]],
    *,
    name: str = "data_type",
) -> str:
    if not isinstance(data_type, str) or not data_type.strip():
        raise CapabilityError(f"{name}_required", allowed=sorted(registry))
    normalized = data_type.strip()
    if normalized not in registry:
        raise CapabilityError(f"{name}_not_allowed", allowed=sorted(registry))
    return normalized


def google_health_filter_date_part(civil_time: str) -> str:
    return civil_time.split("T", maxsplit=1)[0]


def google_health_filter_expression(spec: dict[str, Any], start: str, end: str) -> str:
    prefix = spec["filter_prefix"]
    filter_kind = spec["filter_kind"]
    if filter_kind == "interval":
        return (
            f'{prefix}.interval.civil_start_time >= "{start}" '
            f'AND {prefix}.interval.civil_start_time < "{end}"'
        )
    if filter_kind == "sample":
        return (
            f'{prefix}.sample_time.civil_time >= "{start}" '
            f'AND {prefix}.sample_time.civil_time < "{end}"'
        )
    if filter_kind == "daily":
        return (
            f'{prefix}.date >= "{google_health_filter_date_part(start)}" '
            f'AND {prefix}.date < "{google_health_filter_date_part(end)}"'
        )
    if filter_kind == "session_civil_start":
        return (
            f'{prefix}.interval.civil_start_time >= "{start}" '
            f'AND {prefix}.interval.civil_start_time < "{end}"'
        )
    if filter_kind == "sleep_session_civil_end":
        return (
            f'{prefix}.interval.civil_end_time >= "{start}" '
            f'AND {prefix}.interval.civil_end_time < "{end}"'
        )
    raise CapabilityError("google_health_filter_kind_not_supported", filter_kind=filter_kind)


def google_health_page_size_for_spec(
    value: int | None,
    spec: dict[str, Any],
    *,
    default: int = 25,
    maximum: int = 100,
) -> int:
    max_page_size = spec.get("max_page_size")
    effective_maximum = min(maximum, max_page_size) if isinstance(max_page_size, int) else maximum
    return normalize_page_size(value, default=min(default, effective_maximum), minimum=1, maximum=effective_maximum)


def google_health_rollup_field(data_type: str) -> str:
    spec = GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES[data_type]
    return str(spec.get("daily_rollup_field") or spec["field"])


def google_health_health_rollup_data_types() -> dict[str, dict[str, Any]]:
    return {
        name: spec
        for name, spec in GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES.items()
        if spec["scope"] == GOOGLE_HEALTH_HEALTH_METRICS_SCOPE or name == "heart-rate"
    }


def google_health_daily_health_metric_data_types() -> dict[str, dict[str, Any]]:
    return {
        name: spec
        for name, spec in GOOGLE_HEALTH_HEALTH_METRIC_DATA_TYPES.items()
        if spec["filter_kind"] == "daily"
    }


def google_health_exercise_data_point_id_from_name(resource_name: Any) -> str | None:
    if not isinstance(resource_name, str):
        return None
    parts = resource_name.split("/")
    if len(parts) != 6:
        return None
    if parts[0] != "users" or parts[2] != "dataTypes" or parts[3] != "exercise" or parts[4] != "dataPoints":
        return None
    data_point_id = parts[5]
    return data_point_id if GOOGLE_HEALTH_EXERCISE_DATA_POINT_ID_RE.fullmatch(data_point_id) else None


def normalize_google_health_exercise_data_point_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("exercise_data_point_id_required")
    normalized = value.strip()
    if normalized.startswith("users/"):
        extracted = google_health_exercise_data_point_id_from_name(normalized)
        if extracted:
            return extracted
    if not GOOGLE_HEALTH_EXERCISE_DATA_POINT_ID_RE.fullmatch(normalized) or "/" in normalized:
        raise CapabilityError("exercise_data_point_id_invalid")
    return normalized


def sanitize_google_health_data_point(data_point: Any, *, expose_exercise_id: bool = False) -> Any:
    if not isinstance(data_point, dict):
        return data_point
    data_point_id = google_health_exercise_data_point_id_from_name(data_point.get("name")) if expose_exercise_id else None
    sanitized = redacted_json(data_point)
    if isinstance(sanitized, dict):
        sanitized.pop("name", None)
        if data_point_id:
            sanitized["data_point_id"] = data_point_id
    return sanitized


def summarize_google_health_exercise(data_point: Any) -> dict[str, Any]:
    if not isinstance(data_point, dict):
        return {}
    exercise = data_point.get("exercise")
    if not isinstance(exercise, dict):
        return {}
    interval = exercise.get("interval") if isinstance(exercise.get("interval"), dict) else {}
    metrics = exercise.get("metricsSummary") if isinstance(exercise.get("metricsSummary"), dict) else {}
    return redacted_json(
        {
            "dataPointId": data_point.get("data_point_id"),
            "displayName": exercise.get("displayName"),
            "exerciseType": exercise.get("exerciseType"),
            "interval": {
                "civilStartTime": interval.get("civilStartTime") if isinstance(interval, dict) else None,
                "civilEndTime": interval.get("civilEndTime") if isinstance(interval, dict) else None,
                "startTime": interval.get("startTime") if isinstance(interval, dict) else None,
                "endTime": interval.get("endTime") if isinstance(interval, dict) else None,
            },
            "activeDuration": exercise.get("activeDuration"),
            "metricsSummary": {
                key: metrics.get(key)
                for key in (
                    "steps",
                    "distanceMillimeters",
                    "caloriesKcal",
                    "activeZoneMinutes",
                    "averageHeartRateBeatsPerMinute",
                    "averagePaceSecondsPerMeter",
                    "averageSpeedMillimetersPerSecond",
                    "elevationGainMillimeters",
                )
                if key in metrics
            },
        }
    )


def summarize_google_health_sleep(data_point: Any) -> dict[str, Any]:
    if not isinstance(data_point, dict):
        return {}
    sleep = data_point.get("sleep")
    if not isinstance(sleep, dict):
        return {}
    stages = sleep.get("stages") if isinstance(sleep.get("stages"), list) else []
    stage_counts: dict[str, int] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_type = stage.get("stageType") or stage.get("type")
        if isinstance(stage_type, str) and stage_type:
            stage_counts[stage_type] = stage_counts.get(stage_type, 0) + 1

    out_of_bed_segments = sleep.get("outOfBedSegments") if isinstance(sleep.get("outOfBedSegments"), list) else []
    return redacted_json(
        {
            "type": sleep.get("type"),
            "interval": sleep.get("interval"),
            "summary": sleep.get("summary"),
            "metadata": sleep.get("metadata"),
            "stage_count": len(stages),
            "stage_types": stage_counts,
            "out_of_bed_segment_count": len(out_of_bed_segments),
        }
    )


def google_health_activity_data_types_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "required_readonly_scopes": list(GOOGLE_HEALTH_REQUIRED_READONLY_SCOPES),
        "optional_readonly_scopes_prepared": list(GOOGLE_HEALTH_OPTIONAL_READONLY_SCOPES),
        "source": {
            "discovery": "https://health.googleapis.com/$discovery/rest?version=v4",
            "data_points_list": "https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/list",
            "daily_rollup": "https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/dailyRollUp",
            "tcx_export": "https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/exportExerciseTcx",
        },
        "notes": [
            "Google Health v4 does not expose a generic users.dataTypes.list endpoint.",
            "Supported names are documented through the DataPoint and DailyRollupDataPoint union fields.",
            "Path data type names are hyphenated, while list filter prefixes use the documented snake_case field names.",
            "OAuth scope changes require a new user-consented refresh token in OpenBao; tools return sanitized Google API errors if a scope or data type is unavailable.",
            "TCX export is explicit and stores the XML as a private runtime artifact; it requires activity_and_fitness.readonly plus location.readonly.",
        ],
        "activity_data_points": google_health_data_type_payload(GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES),
        "sleep_data_points": google_health_data_type_payload(GOOGLE_HEALTH_SLEEP_DATA_TYPES),
        "health_metric_data_points": google_health_data_type_payload(GOOGLE_HEALTH_HEALTH_METRIC_DATA_TYPES),
        "daily_rollup_data_types": {
            name: {
                "field": spec["field"],
                "required_scope": spec["scope"],
                "description": spec["description"],
            }
            for name, spec in sorted(GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES.items())
        },
        "tcx_export": {
            "tool": "google_health.export_exercise_tcx",
            "required_scopes": [GOOGLE_HEALTH_ACTIVITY_SCOPE, GOOGLE_HEALTH_LOCATION_SCOPE],
            "input": "exercise_data_point_id from google_health.get_exercise_data_points or google_health.get_activity_data_points(data_type='exercise')",
            "response": "private artifact metadata only",
        },
        "recommended_tools": [
            "google_health.get_activity_data_points for paginated activity, steps, distance, calories, active minutes, heart rate, VO2, altitude, and route-adjacent metrics",
            "google_health.get_exercise_data_points for paginated exercise/workout sessions in a date range",
            "google_health.export_exercise_tcx for workout route/TCX export artifacts when location scope is available",
            "google_health.get_sleep_data_points and google_health.summarize_sleep_day for sleep sessions, sleep stages, recovery, and daily log use",
            "google_health.get_health_metric_data_points and google_health.summarize_health_day for heart rate, resting HR, HRV, SpO2, respiratory rate, weight, body fat, temperature, and recovery metrics",
            "google_health.summarize_activity_day for daily log, brain, and fitness data sync summaries",
        ],
    }


async def google_health_list_data_points(
    access_token: str,
    *,
    data_type: str,
    filter_expression: str,
    page_size: int,
    page_token: str | None,
    required_scope: str | None = None,
    expose_exercise_ids: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "filter": filter_expression,
        "pageSize": page_size,
    }
    if page_token:
        params["pageToken"] = page_token

    endpoint = f"https://health.googleapis.com/v4/users/me/dataTypes/{data_type}/dataPoints"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("google_health_data_points_request_failed", **sanitized_api_error(response))

    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise CapabilityError("google_health_data_points_invalid_json") from error

    data_points = payload.get("dataPoints", [])
    if not isinstance(data_points, list):
        data_points = []

    result: dict[str, Any] = {
        "ok": True,
        "endpoint": "google_health.data_points.list",
        "data_type": data_type,
        "filter": filter_expression,
        "page_size": page_size,
        "data_point_count": len(data_points),
        "data_points": [sanitize_google_health_data_point(item, expose_exercise_id=expose_exercise_ids) for item in data_points],
    }
    if required_scope:
        result["required_scope"] = required_scope
    next_page_token = payload.get("nextPageToken")
    if isinstance(next_page_token, str) and next_page_token:
        result["next_page_token"] = next_page_token
    return result


async def google_health_list_data_points_or_error(
    access_token: str,
    *,
    data_type: str,
    filter_expression: str,
    page_size: int,
    page_token: str | None = None,
    required_scope: str | None = None,
    expose_exercise_ids: bool = False,
) -> dict[str, Any]:
    try:
        return await google_health_list_data_points(
            access_token,
            data_type=data_type,
            filter_expression=filter_expression,
            page_size=page_size,
            page_token=page_token,
            required_scope=required_scope,
            expose_exercise_ids=expose_exercise_ids,
        )
    except CapabilityError as error:
        return capability_error_payload(error)


async def google_health_daily_rollup(
    access_token: str,
    *,
    data_type: str,
    target_date: date,
) -> dict[str, Any]:
    next_day = target_date + timedelta(days=1)
    endpoint = f"https://health.googleapis.com/v4/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
    body = {
        "range": {
            "start": {"date": google_health_date_json(target_date)},
            "end": {"date": google_health_date_json(next_day)},
        },
        "windowSizeDays": 1,
        "pageSize": 1,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
        )

    if response.status_code != 200:
        return {
            "ok": False,
            "endpoint": "google_health.data_points.daily_rollup",
            "data_type": data_type,
            "error": sanitized_api_error(response),
        }

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {
            "ok": False,
            "endpoint": "google_health.data_points.daily_rollup",
            "data_type": data_type,
            "status": "invalid_json",
        }

    rollups = payload.get("rollupDataPoints", [])
    if not isinstance(rollups, list):
        rollups = []
    field_name = google_health_rollup_field(data_type)
    first = rollups[0] if rollups and isinstance(rollups[0], dict) else {}
    value = first.get(field_name) if isinstance(first, dict) else None
    return {
        "ok": True,
        "endpoint": "google_health.data_points.daily_rollup",
        "data_type": data_type,
        "field": field_name,
        "required_scope": GOOGLE_HEALTH_DAILY_ROLLUP_DATA_TYPES[data_type]["scope"],
        "rollup_count": len(rollups),
        "value": redacted_json(value),
    }


async def summarize_google_health_activity_day(access_token: str, target_date: date) -> dict[str, Any]:
    rollups: dict[str, Any] = {}
    for data_type in GOOGLE_HEALTH_DAILY_ACTIVITY_DATA_TYPES:
        rollups[data_type] = await google_health_daily_rollup(access_token, data_type=data_type, target_date=target_date)

    next_day = target_date + timedelta(days=1)
    exercise_spec = GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES["exercise"]
    exercises = await google_health_list_data_points(
        access_token,
        data_type="exercise",
        filter_expression=google_health_filter_expression(exercise_spec, target_date.isoformat(), next_day.isoformat()),
        page_size=10,
        page_token=None,
        required_scope=exercise_spec["scope"],
        expose_exercise_ids=True,
    )
    exercise_points = exercises.get("data_points", [])
    exercise_summaries = [
        summarize_google_health_exercise(item)
        for item in exercise_points
        if isinstance(item, dict)
    ]

    return {
        "ok": True,
        "endpoint": "google_health.activity_day_summary",
        "date": target_date.isoformat(),
        "scope": GOOGLE_HEALTH_ACTIVITY_SCOPE,
        "daily_rollups": rollups,
        "exercise": {
            "data_point_count": exercises.get("data_point_count"),
            "has_more": bool(exercises.get("next_page_token")),
            "summaries": exercise_summaries,
        },
    }


async def summarize_google_health_sleep_day(access_token: str, target_date: date) -> dict[str, Any]:
    next_day = target_date + timedelta(days=1)
    sleep_spec = GOOGLE_HEALTH_SLEEP_DATA_TYPES["sleep"]
    sleep_points = await google_health_list_data_points(
        access_token,
        data_type="sleep",
        filter_expression=google_health_filter_expression(sleep_spec, target_date.isoformat(), next_day.isoformat()),
        page_size=10,
        page_token=None,
        required_scope=sleep_spec["scope"],
    )
    data_points = sleep_points.get("data_points", [])
    summaries = [
        summarize_google_health_sleep(item)
        for item in data_points
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "endpoint": "google_health.sleep_day_summary",
        "date": target_date.isoformat(),
        "scope": GOOGLE_HEALTH_SLEEP_SCOPE,
        "sleep": {
            "data_point_count": sleep_points.get("data_point_count"),
            "has_more": bool(sleep_points.get("next_page_token")),
            "summaries": summaries,
        },
    }


async def summarize_google_health_health_day(access_token: str, target_date: date) -> dict[str, Any]:
    rollups: dict[str, Any] = {}
    for data_type in google_health_health_rollup_data_types():
        rollups[data_type] = await google_health_daily_rollup(access_token, data_type=data_type, target_date=target_date)

    next_day = target_date + timedelta(days=1)
    daily_metrics: dict[str, Any] = {}
    for data_type, spec in google_health_daily_health_metric_data_types().items():
        daily_metrics[data_type] = await google_health_list_data_points_or_error(
            access_token,
            data_type=data_type,
            filter_expression=google_health_filter_expression(spec, target_date.isoformat(), next_day.isoformat()),
            page_size=5,
            required_scope=spec["scope"],
        )

    return {
        "ok": True,
        "endpoint": "google_health.health_day_summary",
        "date": target_date.isoformat(),
        "scope": GOOGLE_HEALTH_HEALTH_METRICS_SCOPE,
        "daily_rollups": rollups,
        "daily_metric_records": daily_metrics,
    }


async def export_google_health_exercise_tcx(
    access_token: str,
    *,
    exercise_data_point_id: str,
    partial_data: bool | None,
    resource_url: str,
) -> dict[str, Any]:
    endpoint = (
        "https://health.googleapis.com/v4/users/me/dataTypes/exercise/"
        f"dataPoints/{exercise_data_point_id}:exportExerciseTcx"
    )
    params: dict[str, Any] = {"alt": "media"}
    if partial_data is not None:
        params["partialData"] = partial_data

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.garmin.tcx+xml, application/xml"},
            params=params,
        )

    if response.status_code != 200:
        raise CapabilityError("google_health_tcx_export_failed", **sanitized_api_error(response))
    if len(response.content) > MAX_ARTIFACT_DOWNLOAD_BYTES:
        raise CapabilityError(
            "google_health_tcx_artifact_too_large",
            byte_size=len(response.content),
            max_artifact_bytes=MAX_ARTIFACT_DOWNLOAD_BYTES,
        )

    content_type = response.headers.get("content-type", "application/vnd.garmin.tcx+xml")
    if is_json_content_type(content_type):
        raise CapabilityError(
            "google_health_tcx_unexpected_json_response",
            message="TCX export returned JSON despite alt=media; refusing to inline or store it as route data.",
        )

    artifact = store_artifact(
        response.content,
        "application/vnd.garmin.tcx+xml",
        {
            "kind": "google_health_exercise_tcx",
            "exercise_data_point_id": exercise_data_point_id,
            "required_scopes": [GOOGLE_HEALTH_ACTIVITY_SCOPE, GOOGLE_HEALTH_LOCATION_SCOPE],
        },
    )
    download_url = artifact_download_url(resource_url, artifact["artifact_id"])
    return {
        "ok": True,
        "endpoint": "google_health.exercise_tcx_export",
        "artifact_id": artifact["artifact_id"],
        "download_url": download_url,
        "mime_type": artifact["mime_type"],
        "byte_size": artifact["byte_size"],
        "sha256": artifact["sha256"],
        "created_at": artifact["created_at"],
        "exercise_data_point_id": exercise_data_point_id,
        "required_scopes": [GOOGLE_HEALTH_ACTIVITY_SCOPE, GOOGLE_HEALTH_LOCATION_SCOPE],
    }


async def service_request(
    *,
    base_url: str,
    path: str,
    method: str,
    headers: dict[str, str],
    resource_url: str,
    service_name: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    form_body: dict[str, Any] | None,
    response_mode: str,
    confirm: bool,
    dry_run: bool,
) -> dict[str, Any]:
    normalized_method = normalize_service_method(method)
    normalized_response_mode = normalize_response_mode(response_mode)
    params = normalize_mapping(params, "params")
    json_body = normalize_mapping(json_body, "json_body")
    form_body = normalize_mapping(form_body, "form_body")
    if json_body is not None and form_body is not None:
        raise CapabilityError("only_one_body_format_allowed")

    url = join_service_url(base_url, path)
    if dry_run:
        return dry_run_payload(normalized_method, url)

    ensure_write_confirmed(normalized_method, confirm)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response, oversized_payload = await limited_service_response(
            client,
            method=normalized_method,
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            form_body=form_body,
            mode=normalized_response_mode,
        )
    if oversized_payload is not None:
        return oversized_payload
    if response is None:
        raise CapabilityError("provider_response_missing")

    return service_response_payload(
        response,
        response_mode=normalized_response_mode,
        resource_url=resource_url,
        artifact_metadata={
            "provider": service_name,
            "method": normalized_method,
            "path": validate_service_path(path),
        },
    )


async def freshrss_auth_token(api_url: str, username: str, api_password: str) -> str:
    login_url = join_service_url(api_url, "/accounts/ClientLogin")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            login_url,
            data={
                "Email": username,
                "Passwd": api_password,
            },
        )

    if response.status_code != 200:
        raise CapabilityError("freshrss_auth_failed", **sanitized_api_error(response))

    for line in response.text.splitlines():
        key, _, value = line.partition("=")
        if key == "Auth" and value:
            return value

    raise CapabilityError("freshrss_auth_token_missing")


def legacy_elevenlabs_operation(operation: str) -> dict[str, str] | None:
    spec = LEGACY_ELEVENLABS_OPERATIONS.get(operation)
    if spec is None:
        return None
    return {
        "method": spec["method"],
        "path": spec["path"],
    }


async def run_elevenlabs_text_to_speech(
    api_key: str,
    resource_url: str,
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    stability: float | None,
    similarity_boost: float | None,
    style: float | None,
    use_speaker_boost: bool | None,
    confirm: bool,
) -> dict[str, Any]:
    ensure_write_confirmed("POST", confirm)

    text = text.strip()
    voice_id = voice_id.strip()
    model_id = model_id.strip()
    output_format = output_format.strip()

    if not text:
        raise CapabilityError("elevenlabs_text_required")
    if len(text) > 5000:
        raise CapabilityError("elevenlabs_text_too_large", max_chars=5000)
    if not ELEVENLABS_VOICE_ID_RE.fullmatch(voice_id):
        raise CapabilityError("elevenlabs_invalid_voice_id")
    if not model_id:
        raise CapabilityError("elevenlabs_model_id_required")
    if not output_format:
        raise CapabilityError("elevenlabs_output_format_required")

    body: dict[str, Any] = {
        "text": text,
        "model_id": model_id,
    }
    voice_settings: dict[str, Any] = {}
    if stability is not None:
        voice_settings["stability"] = stability
    if similarity_boost is not None:
        voice_settings["similarity_boost"] = similarity_boost
    if style is not None:
        voice_settings["style"] = style
    if use_speaker_boost is not None:
        voice_settings["use_speaker_boost"] = use_speaker_boost
    if voice_settings:
        body["voice_settings"] = voice_settings

    async with httpx.AsyncClient(timeout=60.0) as client:
        response, oversized_payload = await limited_service_response(
            client,
            method="POST",
            url=f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            params={"output_format": output_format},
            headers={
                "xi-api-key": api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json_body=body,
            form_body=None,
            mode="artifact",
        )
    if oversized_payload is not None:
        return oversized_payload
    if response is None:
        raise CapabilityError("provider_response_missing")

    if response.status_code != 200:
        return {
            "ok": False,
            "http_status": response.status_code,
            "error": sanitized_api_error(response),
        }
    if len(response.content) > MAX_ARTIFACT_DOWNLOAD_BYTES:
        return {
            "ok": False,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "byte_size": len(response.content),
            "headers": safe_response_headers(response),
            "error": "response_too_large_for_artifact",
            "max_artifact_bytes": MAX_ARTIFACT_DOWNLOAD_BYTES,
        }

    mime_header = response.headers.get("content-type")
    mime_type = mime_header.split(";", maxsplit=1)[0].strip() if mime_header else ""
    if not mime_type.lower().startswith("audio/"):
        raise CapabilityError(
            "elevenlabs_unexpected_content_type",
            content_type=mime_type or None,
            http_status=response.status_code,
        )
    artifact = store_artifact(
        response.content,
        mime_type,
        {
            "voice_id": voice_id,
            "model_id": model_id,
            "output_format": output_format,
            "provider": "elevenlabs",
        },
    )
    artifact["download"] = {
        "url": artifact_download_url(resource_url, artifact["artifact_id"]),
        "authorization": "Bearer token required; same private MCP bearer token",
        "exposure": "same private MCP HTTP service; Traefik remains disabled",
    }
    return {"ok": True, **artifact}


def capability_error_payload(error: CapabilityError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error.code,
        **redacted_json(error.details),
    }


def openbao_error_payload(error: OpenBaoError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": error.code,
    }
    if error.status_code is not None:
        payload["openbao_status"] = error.status_code
    return payload


def bearer_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def artifact_response_for(artifact_id: str) -> FileResponse | JSONResponse:
    metadata = read_artifact_metadata(artifact_id)
    if metadata is None:
        return JSONResponse({"ok": False, "error": "artifact_not_found"}, status_code=404)

    byte_size = metadata.get("byte_size")
    if not isinstance(byte_size, int) or byte_size > MAX_ARTIFACT_DOWNLOAD_BYTES:
        return JSONResponse({"ok": False, "error": "artifact_too_large"}, status_code=413)

    filename = metadata.get("filename")
    mime_type = metadata.get("mime_type")
    if not isinstance(filename, str) or not isinstance(mime_type, str):
        return JSONResponse({"ok": False, "error": "artifact_metadata_invalid"}, status_code=500)

    path = artifact_dir() / filename
    if not path.exists() or not path.is_file():
        return JSONResponse({"ok": False, "error": "artifact_file_missing"}, status_code=404)

    return FileResponse(path, media_type=mime_type, filename=filename)


def build_mcp() -> FastMCP:
    token = require_env("HEISENBERG_ACCESS_MCP_TOKEN")
    resource_url = os.environ.get("HEISENBERG_ACCESS_MCP_RESOURCE_URL", "http://127.0.0.1:8020/mcp")
    openbao_addr = os.environ.get("OPENBAO_ADDR", "http://openbao-app:8200")
    openbao_token = os.environ.get("OPENBAO_TOKEN", "").strip() or None
    openbao = OpenBaoKV2(openbao_addr, openbao_token)

    mcp = FastMCP(
        name="heisenberg-access-mcp",
        host=os.environ.get("HEISENBERG_ACCESS_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEISENBERG_ACCESS_MCP_PORT", "8000")),
        stateless_http=True,
        token_verifier=StaticBearerTokenVerifier(token),
        auth=AuthSettings(
            issuer_url=resource_url,
            resource_server_url=resource_url,
            required_scopes=["mcp:call"],
        ),
        transport_security=transport_security_settings(resource_url),
    )

    @mcp.tool()
    async def access_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="access_status", client_id="heisenberg-access-mcp-env-token")
        return {
            "service": "heisenberg-access-mcp",
            "ok": True,
            "openbao": openbao_target_summary(openbao_addr),
            "capabilities": CAPABILITIES,
        }

    @mcp.tool()
    async def openbao_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="openbao_status", client_id="heisenberg-access-mcp-env-token")
        try:
            return await read_openbao_health(openbao_addr, openbao_token)
        except httpx.HTTPError as error:
            emit_event("openbao_status_error", error_type=type(error).__name__)
            return {
                "reachable": False,
                "ready": False,
                "error": type(error).__name__,
            }

    @mcp.tool(name="x.get_tweet")
    async def x_get_tweet(ctx: Context, tweet_id_or_url: str) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="x.get_tweet", client_id="heisenberg-access-mcp-env-token")
        try:
            tweet_id = extract_tweet_id(tweet_id_or_url)
            oauth = await openbao.read("x_oauth")
            if expires_soon(oauth.get("expires_at")):
                oauth = await refresh_x_access_token(openbao, oauth)

            try:
                return await fetch_x_tweet(required_secret_value(oauth, "access_token"), tweet_id)
            except CapabilityError as error:
                if error.code != "x_tweet_request_failed" or error.details.get("http_status") != 401:
                    raise
                refreshed = await refresh_x_access_token(openbao, oauth)
                return await fetch_x_tweet(required_secret_value(refreshed, "access_token"), tweet_id)
        except OpenBaoError as error:
            emit_event("capability_error", tool="x.get_tweet", error=error.code, openbao_status=error.status_code)
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="x.get_tweet", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="x.get_tweet", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="x.list_bookmarks")
    async def x_list_bookmarks(
        ctx: Context,
        page_size: int | None = None,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """List Tim's current X bookmarks through server-side OAuth, paginated for Brain ingest."""
        emit_event("mcp_tool_call", tool="x.list_bookmarks", client_id="heisenberg-access-mcp-env-token")
        try:
            effective_page_size = normalize_page_size(
                page_size,
                default=X_BOOKMARK_DEFAULT_PAGE_SIZE,
                minimum=1,
                maximum=X_BOOKMARK_MAX_PAGE_SIZE,
            )
            effective_pagination_token = normalize_x_pagination_token(pagination_token)

            oauth = await openbao.read("x_oauth")
            if expires_soon(oauth.get("expires_at")):
                oauth = await refresh_x_access_token(openbao, oauth)
            oauth, user_id = await ensure_x_user_id(openbao, oauth)

            try:
                return await fetch_x_bookmarks(
                    required_secret_value(oauth, "access_token"),
                    user_id,
                    page_size=effective_page_size,
                    pagination_token=effective_pagination_token,
                )
            except CapabilityError as error:
                if error.code != "x_bookmarks_request_failed" or error.details.get("http_status") != 401:
                    raise
                oauth = await refresh_x_access_token(openbao, oauth)
                oauth, user_id = await ensure_x_user_id(openbao, oauth)
                return await fetch_x_bookmarks(
                    required_secret_value(oauth, "access_token"),
                    user_id,
                    page_size=effective_page_size,
                    pagination_token=effective_pagination_token,
                )
        except OpenBaoError as error:
            emit_event("capability_error", tool="x.list_bookmarks", error=error.code, openbao_status=error.status_code)
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="x.list_bookmarks", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="x.list_bookmarks", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="x.unbookmark_tweets")
    async def x_unbookmark_tweets(
        ctx: Context,
        tweet_ids_or_urls: list[str],
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove X bookmarks after successful ingest; requires confirm=true unless dry_run=true."""
        emit_event("mcp_tool_call", tool="x.unbookmark_tweets", client_id="heisenberg-access-mcp-env-token")
        try:
            tweet_ids = normalize_x_tweet_ids(tweet_ids_or_urls)
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "method": "DELETE",
                    "endpoint": "x.bookmarks.delete",
                    "tweet_ids": tweet_ids,
                    "count": len(tweet_ids),
                    "requires_confirm": True,
                }

            ensure_write_confirmed("DELETE", confirm)
            oauth = await openbao.read("x_oauth")
            if expires_soon(oauth.get("expires_at")):
                oauth = await refresh_x_access_token(openbao, oauth)
            oauth, user_id = await ensure_x_user_id(openbao, oauth)
            access_token = required_secret_value(oauth, "access_token")

            results: list[dict[str, Any]] = []
            refreshed_after_401 = False
            for tweet_id in tweet_ids:
                try:
                    results.append(await delete_x_bookmark(access_token, user_id, tweet_id))
                except CapabilityError as error:
                    if (
                        error.code == "x_unbookmark_request_failed"
                        and error.details.get("http_status") == 401
                        and not refreshed_after_401
                    ):
                        oauth = await refresh_x_access_token(openbao, oauth)
                        oauth, user_id = await ensure_x_user_id(openbao, oauth)
                        access_token = required_secret_value(oauth, "access_token")
                        refreshed_after_401 = True
                        try:
                            results.append(await delete_x_bookmark(access_token, user_id, tweet_id))
                            continue
                        except CapabilityError as retry_error:
                            error = retry_error
                    payload = capability_error_payload(error)
                    payload["tweet_id"] = tweet_id
                    results.append(payload)

            deleted_count = sum(1 for result in results if result.get("ok") is True and result.get("bookmarked") is False)
            return {
                "ok": all(result.get("ok") is True for result in results),
                "endpoint": "x.bookmarks.delete",
                "count": len(tweet_ids),
                "deleted_count": deleted_count,
                "results": results,
            }
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="x.unbookmark_tweets",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="x.unbookmark_tweets", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="x.unbookmark_tweets", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.access_status")
    async def google_health_access_status(ctx: Context) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="google_health.access_status", client_id="heisenberg-access-mcp-env-token")
        try:
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            status = await fetch_google_health_access_status(access_token)
            status["token_metadata_updated"] = wrote_metadata
            return status
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.access_status",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.access_status", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.access_status", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.list_data_types")
    async def google_health_list_data_types(ctx: Context) -> dict[str, Any]:
        """List documented Google Health fitness, exercise, workout, activity, sleep, health metrics, heart rate, HRV, recovery, weight, body fat, oxygen saturation, respiratory rate, route, location, TCX, health datapoints, date range tools, and required readonly scopes."""
        emit_event("mcp_tool_call", tool="google_health.list_data_types", client_id="heisenberg-access-mcp-env-token")
        return google_health_activity_data_types_payload()

    @mcp.tool(name="google_health.get_activity_data_points")
    async def google_health_get_activity_data_points(
        ctx: Context,
        data_type: str = "steps",
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Read paginated Google Health activity fitness data points by allowlisted data_type for daily log and activity sync: steps, distance, calories, active minutes, heart rate, heart-rate zones, VO2 max, altitude, exercise/workout, and route-adjacent metrics."""
        emit_event("mcp_tool_call", tool="google_health.get_activity_data_points", client_id="heisenberg-access-mcp-env-token")
        try:
            normalized_data_type = normalize_google_health_data_type(data_type, GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES)
            spec = GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES[normalized_data_type]
            start = normalize_google_health_civil_time(
                start_time,
                default=google_health_default_start_date(30),
                name="start_time",
            )
            end = normalize_google_health_civil_time(
                end_time,
                default=google_health_default_end_date(),
                name="end_time",
            )
            normalized_page_size = google_health_page_size_for_spec(page_size, spec)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await google_health_list_data_points(
                access_token,
                data_type=normalized_data_type,
                filter_expression=google_health_filter_expression(spec, start, end),
                page_size=normalized_page_size,
                page_token=page_token.strip() if isinstance(page_token, str) and page_token.strip() else None,
                required_scope=spec["scope"],
                expose_exercise_ids=normalized_data_type == "exercise",
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.get_activity_data_points",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.get_activity_data_points", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.get_activity_data_points", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.get_exercise_data_points")
    async def google_health_get_exercise_data_points(
        ctx: Context,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Read paginated Google Health exercise/workout fitness sessions for a civil date range; useful for daily log, activity sync, workout history, route/TCX export IDs, distance, calories, steps, and health datapoints context."""
        emit_event("mcp_tool_call", tool="google_health.get_exercise_data_points", client_id="heisenberg-access-mcp-env-token")
        try:
            spec = GOOGLE_HEALTH_ACTIVITY_RAW_DATA_TYPES["exercise"]
            start = normalize_google_health_civil_time(
                start_time,
                default=google_health_default_start_date(30),
                name="start_time",
            )
            end = normalize_google_health_civil_time(
                end_time,
                default=google_health_default_end_date(),
                name="end_time",
            )
            normalized_page_size = google_health_page_size_for_spec(page_size, spec, default=10)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await google_health_list_data_points(
                access_token,
                data_type="exercise",
                filter_expression=google_health_filter_expression(spec, start, end),
                page_size=normalized_page_size,
                page_token=page_token.strip() if isinstance(page_token, str) and page_token.strip() else None,
                required_scope=spec["scope"],
                expose_exercise_ids=True,
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.get_exercise_data_points",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.get_exercise_data_points", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.get_exercise_data_points", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.export_exercise_tcx")
    async def google_health_export_exercise_tcx(
        ctx: Context,
        exercise_data_point_id: str,
        partial_data: bool | None = None,
    ) -> dict[str, Any]:
        """Export a Google Health exercise/workout route as TCX using an allowlisted exercise data_point_id; read-only, requires activity and location scopes, stores route/location XML as a private artifact and returns metadata only."""
        emit_event("mcp_tool_call", tool="google_health.export_exercise_tcx", client_id="heisenberg-access-mcp-env-token")
        try:
            normalized_data_point_id = normalize_google_health_exercise_data_point_id(exercise_data_point_id)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await export_google_health_exercise_tcx(
                access_token,
                exercise_data_point_id=normalized_data_point_id,
                partial_data=partial_data,
                resource_url=resource_url,
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.export_exercise_tcx",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.export_exercise_tcx", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.export_exercise_tcx", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.get_sleep_data_points")
    async def google_health_get_sleep_data_points(
        ctx: Context,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Read paginated Google Health sleep sessions for sleep, sleep stages, sleep summary, recovery, daily log, health datapoints, and date range queries; page_size is capped at 25."""
        emit_event("mcp_tool_call", tool="google_health.get_sleep_data_points", client_id="heisenberg-access-mcp-env-token")
        try:
            spec = GOOGLE_HEALTH_SLEEP_DATA_TYPES["sleep"]
            start = normalize_google_health_civil_time(
                start_time,
                default=google_health_default_start_date(30),
                name="start_time",
            )
            end = normalize_google_health_civil_time(
                end_time,
                default=google_health_default_end_date(),
                name="end_time",
            )
            normalized_page_size = google_health_page_size_for_spec(page_size, spec, default=10)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await google_health_list_data_points(
                access_token,
                data_type="sleep",
                filter_expression=google_health_filter_expression(spec, start, end),
                page_size=normalized_page_size,
                page_token=page_token.strip() if isinstance(page_token, str) and page_token.strip() else None,
                required_scope=spec["scope"],
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.get_sleep_data_points",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.get_sleep_data_points", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.get_sleep_data_points", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.summarize_activity_day")
    async def google_health_summarize_activity_day(ctx: Context, date: str) -> dict[str, Any]:
        """Summarize one Google Health activity day for daily log / brain / fitness data sync: steps, calories, distance, active minutes, heart rate, heart-rate zones, altitude, floors, VO2 max, exercise, workouts, activity, health datapoints, and date range context."""
        emit_event("mcp_tool_call", tool="google_health.summarize_activity_day", client_id="heisenberg-access-mcp-env-token")
        try:
            target_date = parse_iso_date(date)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await summarize_google_health_activity_day(access_token, target_date)
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.summarize_activity_day",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.summarize_activity_day", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.summarize_activity_day", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.summarize_sleep_day")
    async def google_health_summarize_sleep_day(ctx: Context, date: str) -> dict[str, Any]:
        """Summarize one Google Health sleep day for daily log, brain, recovery, sleep stages, sleep duration, sleep summary, health metrics, and fitness data sync without dumping full raw sleep datapoints."""
        emit_event("mcp_tool_call", tool="google_health.summarize_sleep_day", client_id="heisenberg-access-mcp-env-token")
        try:
            target_date = parse_iso_date(date)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await summarize_google_health_sleep_day(access_token, target_date)
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.summarize_sleep_day",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.summarize_sleep_day", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.summarize_sleep_day", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.get_health_metric_data_points")
    async def google_health_get_health_metric_data_points(
        ctx: Context,
        data_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Read paginated Google Health health metrics by allowlisted data_type for daily log, recovery, heart rate, resting HR, HRV, oxygen saturation / SpO2, respiratory rate, weight, body fat, temperature, blood glucose, and health datapoints date range queries."""
        emit_event("mcp_tool_call", tool="google_health.get_health_metric_data_points", client_id="heisenberg-access-mcp-env-token")
        try:
            normalized_data_type = normalize_google_health_data_type(data_type, GOOGLE_HEALTH_HEALTH_METRIC_DATA_TYPES)
            spec = GOOGLE_HEALTH_HEALTH_METRIC_DATA_TYPES[normalized_data_type]
            start = normalize_google_health_civil_time(
                start_time,
                default=google_health_default_start_date(30),
                name="start_time",
            )
            end = normalize_google_health_civil_time(
                end_time,
                default=google_health_default_end_date(),
                name="end_time",
            )
            normalized_page_size = google_health_page_size_for_spec(page_size, spec)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await google_health_list_data_points(
                access_token,
                data_type=normalized_data_type,
                filter_expression=google_health_filter_expression(spec, start, end),
                page_size=normalized_page_size,
                page_token=page_token.strip() if isinstance(page_token, str) and page_token.strip() else None,
                required_scope=spec["scope"],
            )
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.get_health_metric_data_points",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.get_health_metric_data_points", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.get_health_metric_data_points", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="google_health.summarize_health_day")
    async def google_health_summarize_health_day(ctx: Context, date: str) -> dict[str, Any]:
        """Summarize one Google Health health metrics day for daily log, brain, recovery, heart rate, resting HR, HRV, oxygen saturation / SpO2, respiratory rate, weight, body fat, temperature, blood glucose, and health datapoints."""
        emit_event("mcp_tool_call", tool="google_health.summarize_health_day", client_id="heisenberg-access-mcp-env-token")
        try:
            target_date = parse_iso_date(date)
            access_token, wrote_metadata = await refresh_google_access_token(openbao)
            payload = await summarize_google_health_health_day(access_token, target_date)
            payload["token_metadata_updated"] = wrote_metadata
            return payload
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="google_health.summarize_health_day",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="google_health.summarize_health_day", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="google_health.summarize_health_day", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="elevenlabs.request")
    async def elevenlabs_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/v2/voices",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Request the ElevenLabs API within its fixed service scope; binary/large responses become artifacts."""
        emit_event("mcp_tool_call", tool="elevenlabs.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if operation is not None:
                legacy = legacy_elevenlabs_operation(operation)
                if legacy is None:
                    raise CapabilityError(
                        "elevenlabs_operation_not_known",
                        known_operations=sorted(LEGACY_ELEVENLABS_OPERATIONS),
                    )
                method = legacy["method"]
                path = legacy["path"]
            if dry_run:
                return await service_request(
                    base_url="https://api.elevenlabs.io",
                    path=path,
                    method=method,
                    headers={},
                    resource_url=resource_url,
                    service_name="elevenlabs",
                    params=params,
                    json_body=json_body,
                    form_body=form_body,
                    response_mode=response_mode,
                    confirm=confirm,
                    dry_run=True,
                )
            ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("elevenlabs")
            api_key = required_secret_value(secret, "api_key")
            return await service_request(
                base_url="https://api.elevenlabs.io",
                path=path,
                method=method,
                headers={
                    "xi-api-key": api_key,
                    "Accept": "application/json, audio/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="elevenlabs",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="elevenlabs.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="elevenlabs.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="elevenlabs.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="homeassistant.request")
    async def homeassistant_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/api/",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request Home Assistant within the configured base URL using server-side bearer auth."""
        emit_event("mcp_tool_call", tool="homeassistant.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("homeassistant")
            base_url = required_secret_value(secret, "url")
            token_value = required_secret_value(secret, "token")
            return await service_request(
                base_url=base_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"Bearer {token_value}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="homeassistant",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="homeassistant.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="homeassistant.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="homeassistant.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="freshrss.request")
    async def freshrss_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/reader/api/0/user-info",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request FreshRSS within the configured API base URL using server-side API-password auth."""
        emit_event("mcp_tool_call", tool="freshrss.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("freshrss")
            api_url = required_secret_value(secret, "api_url")
            username = required_secret_value(secret, "username")
            api_password = required_secret_value(secret, "api_password")
            auth_token = "dry-run" if dry_run else await freshrss_auth_token(api_url, username, api_password)
            return await service_request(
                base_url=api_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"GoogleLogin auth={auth_token}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                },
                resource_url=resource_url,
                service_name="freshrss",
                params=params,
                json_body=json_body,
                form_body=form_body,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="freshrss.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="freshrss.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="freshrss.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="tandoor.request")
    async def tandoor_request(
        ctx: Context,
        method: str = "GET",
        path: str = "/api/recipe/",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        response_mode: str = "auto",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Request Tandoor within the configured base URL using server-side bearer auth."""
        emit_event("mcp_tool_call", tool="tandoor.request", client_id="heisenberg-access-mcp-env-token")
        try:
            if not dry_run:
                ensure_write_confirmed(normalize_service_method(method), confirm)
            secret = await openbao.read("tandoor")
            base_url = required_secret_value(secret, "url")
            api_key = required_secret_value(secret, "api_key")
            return await service_request(
                base_url=base_url,
                path=path,
                method=method,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json, text/*;q=0.9, */*;q=0.1",
                    "Accept-Encoding": "identity",
                },
                resource_url=resource_url,
                service_name="tandoor",
                params=params,
                json_body=json_body,
                form_body=None,
                response_mode=response_mode,
                confirm=confirm,
                dry_run=dry_run,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="tandoor.request",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="tandoor.request", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="tandoor.request", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    @mcp.tool(name="elevenlabs.text_to_speech")
    async def elevenlabs_text_to_speech(
        ctx: Context,
        text: str,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        output_format: str = "mp3_44100_128",
        stability: float | None = None,
        similarity_boost: float | None = None,
        style: float | None = None,
        use_speaker_boost: bool | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        emit_event("mcp_tool_call", tool="elevenlabs.text_to_speech", client_id="heisenberg-access-mcp-env-token")
        try:
            ensure_write_confirmed("POST", confirm)
            secret = await openbao.read("elevenlabs")
            api_key = required_secret_value(secret, "api_key")
            return await run_elevenlabs_text_to_speech(
                api_key,
                resource_url,
                text,
                voice_id,
                model_id,
                output_format,
                stability,
                similarity_boost,
                style,
                use_speaker_boost,
                confirm,
            )
        except OpenBaoError as error:
            emit_event(
                "capability_error",
                tool="elevenlabs.text_to_speech",
                error=error.code,
                openbao_status=error.status_code,
            )
            return openbao_error_payload(error)
        except CapabilityError as error:
            emit_event("capability_error", tool="elevenlabs.text_to_speech", error=error.code)
            return capability_error_payload(error)
        except httpx.HTTPError as error:
            emit_event("capability_error", tool="elevenlabs.text_to_speech", error=type(error).__name__)
            return {"ok": False, "error": type(error).__name__}

    return mcp


configure_logging()
mcp_server = build_mcp()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "heisenberg-access-mcp"})


async def download_artifact(request: Request) -> Response:
    expected_token = require_env("HEISENBERG_ACCESS_MCP_TOKEN")
    provided_token = bearer_token_from_request(request)
    if provided_token is None or not hmac.compare_digest(provided_token, expected_token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    return artifact_response_for(request.path_params["artifact_id"])


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp_server.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/artifacts/{artifact_id}", download_artifact, methods=["GET"]),
        Mount("/", app=mcp_server.streamable_http_app()),
    ],
    middleware=[Middleware(AccessLogMiddleware)],
    lifespan=lifespan,
)


def main() -> None:
    host = os.environ.get("HEISENBERG_ACCESS_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("HEISENBERG_ACCESS_MCP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
