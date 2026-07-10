"""Tests for Open-Meteo URL generation."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase

from apps.orchestrator.weather import (
    TIMEZONE_COORDS,
    build_weather_url,
    build_weather_url_from_coords,
    get_coords_for_timezone,
    get_place_label_for_timezone,
    resolve_weather_search_location,
)


class BuildWeatherUrlFromCoordsTest(SimpleTestCase):
    def _query(self, url: str) -> dict[str, list[str]]:
        return parse_qs(urlparse(url).query)

    def test_returns_open_meteo_forecast_endpoint(self):
        url = build_weather_url_from_coords(34.69, 135.50, "Asia/Tokyo")
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.open-meteo.com")
        self.assertEqual(parsed.path, "/v1/forecast")

    def test_includes_current_fields(self):
        url = build_weather_url_from_coords(34.69, 135.50, "Asia/Tokyo")
        current = self._query(url)["current"][0].split(",")
        self.assertIn("temperature_2m", current)
        self.assertIn("weather_code", current)
        self.assertIn("wind_speed_10m", current)

    def test_includes_daily_fields(self):
        url = build_weather_url_from_coords(34.69, 135.50, "Asia/Tokyo")
        daily = self._query(url)["daily"][0].split(",")
        self.assertIn("weather_code", daily)
        self.assertIn("temperature_2m_max", daily)
        self.assertIn("temperature_2m_min", daily)
        self.assertIn("precipitation_probability_max", daily)

    def test_includes_hourly_fields_for_intraday_reporting(self):
        url = build_weather_url_from_coords(34.69, 135.50, "Asia/Tokyo")
        params = self._query(url)
        self.assertIn("hourly", params, "hourly param missing from URL")
        hourly = params["hourly"][0].split(",")
        for field in (
            "temperature_2m",
            "weather_code",
            "precipitation_probability",
            "precipitation",
            "wind_speed_10m",
        ):
            self.assertIn(field, hourly, f"{field!r} missing from hourly fields")

    def test_encodes_coordinates_and_timezone(self):
        url = build_weather_url_from_coords(40.71, -74.01, "America/New_York")
        params = self._query(url)
        self.assertEqual(params["latitude"], ["40.71"])
        self.assertEqual(params["longitude"], ["-74.01"])
        self.assertEqual(params["timezone"], ["America/New_York"])
        self.assertEqual(params["forecast_days"], ["3"])


class BuildWeatherUrlTest(SimpleTestCase):
    def test_uses_mapped_coords_for_known_timezone(self):
        url = build_weather_url("Asia/Tokyo")
        params = parse_qs(urlparse(url).query)
        lat, lon = TIMEZONE_COORDS["Asia/Tokyo"]
        self.assertEqual(params["latitude"], [str(lat)])
        self.assertEqual(params["longitude"], [str(lon)])
        self.assertIn("hourly", params)

    def test_falls_back_to_region_default_for_unknown_timezone(self):
        url = build_weather_url("Asia/Yerevan")  # not in TIMEZONE_COORDS
        params = parse_qs(urlparse(url).query)
        # Asia region default is Osaka
        expected_lat, expected_lon = TIMEZONE_COORDS["Asia/Tokyo"]
        self.assertEqual(params["latitude"], [str(expected_lat)])
        self.assertEqual(params["longitude"], [str(expected_lon)])


class GetCoordsForTimezoneTest(SimpleTestCase):
    def test_returns_mapped_coords(self):
        self.assertEqual(get_coords_for_timezone("Europe/London"), (51.51, -0.13))

    def test_falls_back_to_london_for_etc_gmt(self):
        self.assertEqual(get_coords_for_timezone("Etc/GMT+5"), (51.51, -0.13))

    def test_falls_back_to_region_default(self):
        # Africa region default is Lagos
        self.assertEqual(get_coords_for_timezone("Africa/Casablanca"), (6.52, 3.38))

    def test_falls_back_to_london_for_completely_unknown(self):
        self.assertEqual(get_coords_for_timezone("Mars/Olympus_Mons"), (51.51, -0.13))


class GetPlaceLabelForTimezoneTest(SimpleTestCase):
    """web_search takes a place name, not coordinates (P0-0b: web_fetch is
    now denied, so the morning-briefing weather step no longer builds an
    Open-Meteo URL — it resolves a search-query location label instead)."""

    def test_derives_label_from_timezone_last_segment(self):
        self.assertEqual(get_place_label_for_timezone("America/New_York"), "New York")
        self.assertEqual(get_place_label_for_timezone("Asia/Tokyo"), "Tokyo")
        self.assertEqual(get_place_label_for_timezone("Europe/London"), "London")

    def test_no_slash_returns_whole_string(self):
        self.assertEqual(get_place_label_for_timezone("UTC"), "UTC")

    def test_empty_timezone_falls_back(self):
        self.assertEqual(get_place_label_for_timezone(""), "your area")


class ResolveWeatherSearchLocationTest(SimpleTestCase):
    def test_prefers_profile_city_when_set(self):
        self.assertEqual(resolve_weather_search_location("Osaka", "America/New_York"), "Osaka")

    def test_strips_whitespace_on_profile_city(self):
        self.assertEqual(resolve_weather_search_location("  Brooklyn  ", "UTC"), "Brooklyn")

    def test_falls_back_to_timezone_label_when_city_blank(self):
        self.assertEqual(resolve_weather_search_location("", "Asia/Tokyo"), "Tokyo")

    def test_falls_back_to_timezone_label_when_city_none(self):
        self.assertEqual(resolve_weather_search_location(None, "Europe/Paris"), "Paris")
