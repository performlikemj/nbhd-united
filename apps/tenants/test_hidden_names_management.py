"""Contract tests for hidden-name browsing, name-wide stop, and exact undo."""

from time import perf_counter
from unittest.mock import mock_open, patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.pii.egress import redact_known_values
from apps.pii.entity_registry import canonical_key
from apps.pii.redactor import rehydrate_for_tenant
from apps.tenants.entity_registry_views import _english_words, looks_like_everyday_word
from apps.tenants.models import Tenant, User

URL = "/api/v1/tenants/settings/entity-registry/"
DENY_URL = "/api/v1/tenants/settings/pii-denylist/"


class EverydayWordTests(SimpleTestCase):
    def setUp(self):
        _english_words.cache_clear()
        self.addCleanup(_english_words.cache_clear)

    def test_lowercase_dictionary_entries_are_cached_and_advisory(self):
        words = (
            "gently\nconstellation\ngravity\nswift\nsanity\nretire\nEmma\nTokyo\nUPPER\nMixed\nan\ncan't\n"
            + "a" * 21
            + "\n"
        )
        with patch("apps.tenants.entity_registry_views.Path.open", mock_open(read_data=words)) as opened:
            for name in ("Gently", "Constellation", "Gravity", "Swift", "Sanity", "Retire"):
                with self.subTest(name=name):
                    self.assertTrue(looks_like_everyday_word(name))
            for name in ("Emma", "Tokyo", "Gently Emma"):
                with self.subTest(name=name):
                    self.assertFalse(looks_like_everyday_word(name))
            self.assertEqual(
                _english_words(), frozenset({"gently", "constellation", "gravity", "swift", "sanity", "retire"})
            )
            opened.assert_called_once_with(encoding="utf-8")

    def test_missing_dictionary_silently_caches_empty_fallback(self):
        with patch("apps.tenants.entity_registry_views.Path.open", side_effect=FileNotFoundError) as opened:
            self.assertFalse(looks_like_everyday_word("Gently"))
            self.assertTrue(looks_like_everyday_word("Calendar"))  # Existing fleet rule.
            self.assertTrue(looks_like_everyday_word("A"))  # Existing hygiene rule.
            self.assertEqual(_english_words(), frozenset())
            opened.assert_called_once()


class HiddenNamesManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="hidden-owner", password="test-password")
        cls.tenant = Tenant.objects.create(user=cls.user, status=Tenant.Status.ACTIVE)
        cls.other_user = User.objects.create_user(username="other-owner", password="test-password")
        cls.other = Tenant.objects.create(
            user=cls.other_user,
            status=Tenant.Status.ACTIVE,
            pii_entity_map={"[PERSON_1]": "Other Person"},
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def seed(self, entity_map, denylist=None):
        self.tenant.pii_entity_map = entity_map
        self.tenant.pii_denylist = denylist or {}
        self.tenant.pii_type_counters = {"PERSON": 9999, "LOCATION": 23}
        self.tenant.save(update_fields=["pii_entity_map", "pii_denylist", "pii_type_counters"])
        self.user.tenant = self.tenant

    def post(self, action, placeholders):
        return self.client.post(URL + action + "/", {"placeholders": placeholders}, format="json")

    def test_legacy_get_shape_order_and_unknown_query_unchanged(self):
        self.seed({"[PERSON_2]": "Alice", "[PERSON_1]": {"name": "Zoe"}, "[PERSON_3]": {"retired": True}})
        expected = {
            "entries": [
                {"placeholder": p, "name": n, "relationship": "", "notes": "", "updated_at": None}
                for p, n in [("[PERSON_1]", "Zoe"), ("[PERSON_2]", "Alice")]
            ]
        }
        self.assertEqual(self.client.get(URL).json(), expected)
        self.assertEqual(self.client.get(URL, {"old_client_param": "1"}).json(), expected)

    def test_filters_counts_metadata_and_stopped(self):
        self.seed(
            {
                "[PERSON_1]": {
                    "name": "Calendar",
                    "relationship": "Colleague",
                    "notes": "From PARIS",
                    "reviewed_at": "then",
                },
                "[LOCATION_2]": {"name": "calendar", "provisional": True},
                "[EMAIL_3]": "mail@example.com",
                "[PERSON_4]": {"name": "Zoe", "retired": True},
            }
        )
        for q in ("CALEND", "colleague", "paris", "person_1"):
            with self.subTest(q=q):
                response = self.client.get(URL, {"q": q, "type": "PERSON"}).json()
                self.assertEqual(response["total"], 1)
                self.assertEqual(response["counts"], {"active": 3, "stopped": 1})
                self.assertEqual(response["entries"][0]["state"], "active")
                self.assertTrue(response["entries"][0]["reviewed"])
                self.assertTrue(response["entries"][0]["looks_like_everyday_word"])
                self.assertEqual(response["entries"][0]["persistence"], "permanent")
        location = self.client.get(URL, {"type": "LOCATION"}).json()["entries"][0]
        self.assertEqual(location["persistence"], "provisional")
        self.assertFalse(location["reviewed"])
        self.assertEqual(self.client.get(URL, {"type": "EMAIL"}).json()["total"], 1)
        stopped = self.client.get(URL, {"state": "stopped"}).json()
        self.assertEqual(stopped["entries"][0]["placeholder"], "[PERSON_4]")
        self.assertEqual(stopped["entries"][0]["state"], "stopped")

    def test_everyday_is_advisory_and_uses_both_existing_checks(self):
        self.seed({"[PERSON_1]": "Calendar", "[PERSON_2]": "Quick Delgado", "[PERSON_3]": "A"})
        result = self.client.get(URL, {"everyday": "true"}).json()
        self.assertEqual([e["name"] for e in result["entries"]], ["A", "Calendar"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_1]"], "Calendar")
        self.assertEqual(self.tenant.pii_denylist, {})

    def test_sort_and_cursor_cover_all_entries_once(self):
        self.seed({"[PERSON_9]": "ß", "[LOCATION_1]": "SS", "[PERSON_2]": "alice", "[PERSON_1]": "ALICE"})
        entries = []
        params = {"page_size": 1}
        while True:
            response = self.client.get(URL, params)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["total"], 4)
            entries.extend(e["placeholder"] for e in data["entries"])
            if data["next_cursor"] is None:
                break
            params["cursor"] = data["next_cursor"]
        self.assertEqual(entries, ["[PERSON_1]", "[PERSON_2]", "[LOCATION_1]", "[PERSON_9]"])

    def test_cursor_is_keyset_and_tenant_filter_bound(self):
        self.seed({"[PERSON_1]": "Alice", "[PERSON_2]": "Bob", "[PERSON_3]": "Carol"})
        cursor = self.client.get(URL, {"page_size": 1}).json()["next_cursor"]
        self.seed({"[PERSON_2]": "Bob", "[PERSON_3]": "Carol"})
        self.assertEqual(self.client.get(URL, {"cursor": cursor}).json()["entries"][0]["name"], "Bob")
        self.assertEqual(self.client.get(URL, {"q": "Bob", "cursor": cursor}).status_code, 400)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get(URL, {"cursor": cursor}).status_code, 400)

    def test_default_and_max_page_size(self):
        self.seed({f"[PERSON_{n}]": f"Name {n}" for n in range(201)})
        for size, expected in [(None, 100), (200, 200), (201, 200)]:
            params = {"q": ""} if size is None else {"page_size": size}
            self.assertEqual(len(self.client.get(URL, params).json()["entries"]), expected)

    def test_1400_entry_get_only_computes_needed_flags(self):
        self.seed({f"[PERSON_{n}]": "Gently" for n in range(1400)})
        with (
            patch("apps.tenants.entity_registry_views._english_words", return_value=frozenset({"gently"})),
            patch(
                "apps.tenants.entity_registry_views.looks_like_everyday_word", wraps=looks_like_everyday_word
            ) as flag,
            patch("apps.pii.redactor.is_never_a_name") as fallback,
        ):
            for params, page_size in [({"q": ""}, 100), ({"page_size": 200}, 200), ({"everyday": "false"}, 100)]:
                flag.reset_mock()
                response = self.client.get(URL, params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total"], 1400)
                self.assertEqual(flag.call_count, page_size)
                self.assertTrue(all(e["looks_like_everyday_word"] for e in response.json()["entries"]))
            flag.reset_mock()
            self.assertEqual(self.client.get(URL, {"select_all": "true"}).json()["total"], 1400)
            flag.assert_not_called()
            self.assertEqual(self.client.get(URL, {"everyday": "true"}).json()["total"], 1400)
            self.assertEqual(flag.call_count, 1400)
            fallback.assert_not_called()
        # Advisory browsing must not change whether the word is hidden.
        self.tenant.refresh_from_db()
        self.assertEqual(redact_known_values(self.tenant, "Gently", seam="test"), "[PERSON_0]")

    def test_invalid_query_parameters(self):
        for params in [
            {"cursor": ""},
            {"cursor": "tampered"},
            {"page_size": "no"},
            {"page_size": 0},
            {"page_size": -1},
            {"state": "all"},
            {"everyday": "yes"},
            {"select_all": "yes"},
        ]:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(URL, params).status_code, 400)

    def test_select_all_filters_order_and_limit(self):
        self.seed({f"[PERSON_{n}]": f"Name {n:04d}" for n in range(2001)})
        too_many = self.client.get(URL, {"select_all": "true"})
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(too_many.json(), {"detail": "too_many", "total": 2001})
        selected = self.client.get(URL, {"select_all": "true", "q": "Name 00", "type": "PERSON", "page_size": 1}).json()
        self.assertEqual(selected, {"placeholders": [f"[PERSON_{n}]" for n in range(100)], "total": 100})
        self.post("bulk-stop", ["[PERSON_2000]"])
        self.assertEqual(self.client.get(URL, {"select_all": "true"}).json()["total"], 2000)
        self.assertEqual(
            self.client.get(URL, {"select_all": "true", "state": "stopped"}).json(),
            {"placeholders": ["[PERSON_2000]"], "total": 1},
        )
        self.assertEqual(
            self.client.get(URL, {"select_all": "true", "q": "missing"}).json(), {"placeholders": [], "total": 0}
        )

    def test_stop_name_wide_duplicates_already_retired_and_metadata(self):
        retired = {"name": "aLiCe", "retired": True, "retired_at": "before", "retired_reason": "old", "opaque": 42}
        active = {"name": " ALICE ", "notes": "keep", "provisional": True, "seen": ["a"], "opaque": {"future": True}}
        self.seed(
            {"[PERSON_1]": "Alice", "[LOCATION_2]": active, "[PERSON_3]": retired, "[PERSON_4]": "Bob"},
            {"alice": {"reason": "manual", "decided_at": "original"}},
        )
        response = self.post("bulk-stop", ["[PERSON_3]", "[LOCATION_2]", "missing", "[PERSON_1]"])
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([r["status"] for r in data["results"]], ["stopped", "duplicate", "not_found", "duplicate"])
        self.assertEqual(data["results"][0]["key"], "alice")
        self.assertEqual(data["retired"], ["[PERSON_1]", "[LOCATION_2]"])
        self.assertEqual(data["summary"], {"requested": 4, "names_stopped": 1, "bindings_retired": 2})
        self.tenant.refresh_from_db()
        for field, value in active.items():
            self.assertEqual(self.tenant.pii_entity_map["[LOCATION_2]"][field], value)
        self.assertEqual(self.tenant.pii_entity_map["[LOCATION_2]"]["retired_reason"], "owner")
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_3]"], retired)
        self.assertEqual(self.tenant.pii_denylist["alice"]["decided_at"], "original")
        repeated = self.post("bulk-stop", ["[PERSON_1]"]).json()
        self.assertEqual(repeated["results"][0]["status"], "already_stopped")
        self.assertEqual(repeated["retired"], [])
        self.assertEqual(repeated["summary"]["names_stopped"], 0)

    def test_1084_stop_is_linear_and_fast_then_undo(self):
        entity_map = {f"[PERSON_{n}]": {"name": f"Individual {n}", "opaque": n} for n in range(1084)}
        self.seed(entity_map)
        start = perf_counter()
        with patch("apps.tenants.entity_registry_views.canonical_key", wraps=canonical_key) as canonical:
            with self.assertNumQueries(4):  # savepoint, locked read, update, release
                response = self.post("bulk-stop", list(entity_map))
            self.assertLessEqual(canonical.call_count, 2 * len(entity_map))
        elapsed = perf_counter() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["summary"], {"requested": 1084, "names_stopped": 1084, "bindings_retired": 1084})
        self.assertEqual(len(data["retired"]), 1084)
        self.assertEqual(self.post("restore", data["retired"]).status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, entity_map)
        self.assertEqual(self.tenant.pii_denylist, {})

    def test_stop_restore_redaction_and_historical_rehydration_round_trip(self):
        self.seed({"[PERSON_7]": {"name": "Zelina", "reviewed_at": "then", "opaque": [1]}, "[LOCATION_12]": "Zelina"})
        original = dict(self.tenant.pii_entity_map)
        text = "Ask Zelina today"
        historical = "Ask [PERSON_7] today"
        self.assertEqual(redact_known_values(self.tenant, text, seam="test"), historical)
        self.assertEqual(rehydrate_for_tenant(self.tenant, historical), text)
        stopped = self.post("bulk-stop", ["[PERSON_7]"]).json()
        self.tenant.refresh_from_db()
        self.assertEqual(redact_known_values(self.tenant, text, seam="test"), text)
        self.assertEqual(rehydrate_for_tenant(self.tenant, historical), text)
        restored = self.post("restore", stopped["retired"])
        self.assertEqual([r["status"] for r in restored.json()["results"]], ["restored", "restored"])
        self.tenant.refresh_from_db()
        self.assertEqual(redact_known_values(self.tenant, text, seam="test"), historical)
        self.assertEqual(rehydrate_for_tenant(self.tenant, historical), text)
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_7]"], original["[PERSON_7]"])
        self.assertEqual(self.tenant.pii_denylist, {})
        self.assertEqual(self.tenant.pii_type_counters, {"PERSON": 9999, "LOCATION": 23})

    def test_restore_exact_binding_and_already_active_removes_deny_key(self):
        stopped = {
            "name": "Alice",
            "retired": True,
            "retired_at": "then",
            "retired_reason": "owner",
            "provisional": True,
        }
        self.seed(
            {"[PERSON_1]": stopped, "[LOCATION_2]": stopped, "[PERSON_3]": "Bob"},
            {"alice": {}, "bob": {}, "untouched": {}},
        )
        result = self.post("restore", ["[PERSON_1]", "[PERSON_3]", "[PERSON_1]", "missing"]).json()
        self.assertEqual(
            [r["status"] for r in result["results"]], ["restored", "already_active", "already_active", "not_found"]
        )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_1]"], {"name": "Alice", "provisional": True})
        self.assertEqual(self.tenant.pii_entity_map["[LOCATION_2]"], stopped)
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_3]"], "Bob")
        self.assertEqual(self.tenant.pii_denylist, {"untouched": {}})

    def test_restore_active_entries_is_a_true_noop(self):
        entries = {
            "[PERSON_1]": "Alice",
            "[PERSON_2]": {"name": "Bob", "updated_at": "before", "retired": False, "retired_at": "old", "opaque": [1]},
        }
        self.seed(entries)
        with self.assertNumQueries(3):  # savepoint, locked read, release; no UPDATE
            response = self.post("restore", list(entries))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["status"] for r in response.json()["results"]], ["already_active", "already_active"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, entries)

    def test_restore_active_entry_with_deny_key_only_writes_denylist(self):
        self.seed({"[PERSON_1]": "Alice"}, {"alice": {"reason": "manual"}})
        with self.assertNumQueries(4) as queries:
            response = self.post("restore", ["[PERSON_1]"])
        self.assertEqual(response.json()["results"][0]["status"], "already_active")
        updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn('"pii_denylist"', updates[0])
        self.assertNotIn('"pii_entity_map"', updates[0])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, {"[PERSON_1]": "Alice"})
        self.assertEqual(self.tenant.pii_denylist, {})

    def test_mutation_batch_validation_and_2000_boundary(self):
        for action in ("bulk-stop", "restore"):
            for bad in ([], ["x"] * 2001, [1], [None], [""], [" "], "x", None):
                with self.subTest(action=action, bad_type=type(bad)):
                    self.assertEqual(self.post(action, bad).status_code, 400)
            for body in ([], "invalid", {}, {"placeholders": ["x", {}]}):
                self.assertEqual(self.client.post(URL + action + "/", body, format="json").status_code, 400)
            self.assertEqual(self.post(action, ["missing"] * 2000).status_code, 200)

    def test_authentication_and_tenant_isolation(self):
        self.seed({"[PERSON_1]": "Alice"})
        self.post("bulk-stop", ["[PERSON_1]"])
        self.post("restore", ["[PERSON_1]"])
        self.other.refresh_from_db()
        self.assertEqual(self.other.pii_entity_map, {"[PERSON_1]": "Other Person"})
        self.assertEqual(self.other.pii_denylist, {})
        self.assertEqual(self.client.get(URL, {"q": "Other"}).json()["total"], 0)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL, {"q": ""}).status_code, 401)
        for action in ("bulk-stop", "restore"):
            self.assertEqual(self.post(action, ["[PERSON_1]"]).status_code, 401)

    def test_name_limit_256_patch_and_denylist(self):
        self.seed({"[PERSON_1]": {"name": "Alice", "retired": True, "provisional": True, "opaque": 42}})
        name = "a" * 256
        self.assertEqual(self.client.patch(URL + "[PERSON_1]/", {"name": name}, format="json").status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map["[PERSON_1]"]["opaque"], 42)
        self.assertTrue(self.tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertTrue(self.tenant.pii_entity_map["[PERSON_1]"]["provisional"])
        self.assertEqual(self.client.patch(URL + "[PERSON_1]/", {"name": name + "a"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(DENY_URL, {"name": name}, format="json").status_code, 201)
        self.assertEqual(self.client.post(DENY_URL, {"name": name + "a"}, format="json").status_code, 400)
        bulk = self.client.post(DENY_URL + "bulk/", {"names": [name, name + "a"]}, format="json").json()
        self.assertEqual(bulk["added"], [name])
        self.assertEqual(len(bulk["skipped"]), 1)
