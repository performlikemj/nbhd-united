"""Tests for apps.crypto.cache — per-process DEK cache.

Runs against the stateful `_MOCK_KEK_REGISTRY` in
`apps.orchestrator.azure_client` (AZURE_MOCK=true). Each test mints its own
tenant(s) with a fresh random UUID, so `_CACHE` entries never collide across
tests within the same run (matches the convention in apps/crypto/test_keys.py).
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from apps.crypto import box, cache
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator import azure_client
from apps.tenants.models import Tenant, TenantDek, User


def _create_tenant(*, suffix: str) -> Tenant:
    user = User.objects.create_user(username=f"crypto-cache-{suffix}", password="pass1234")
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, model_tier=Tenant.ModelTier.STARTER)


class _ForceMockAzureMixin:
    """Force `azure_client` into mock mode via patch, never the ambient env var.

    `cache.get_dek` -> `azure_client.unwrap_dek` (and the tests' own
    `purge_kek`/`unwrap_dek` calls) branch on `azure_client._is_mock()`. In
    CI's full suite another test can leave `AZURE_MOCK` unset in
    `os.environ`, so these must not depend on it — patch `_is_mock` -> True
    directly, matching the merged PR1 pattern in
    `apps/orchestrator/test_azure_client_keys.py`. The patch replaces a
    module-level attribute, so it's visible to the worker threads in
    `ConcurrentColdMissTest` too (not thread-local). Started in setUp with
    addCleanup(stop) so it wraps the whole test — including any thread joins.
    """

    def setUp(self):
        super().setUp()
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)


class GetDekColdMissTest(_ForceMockAzureMixin, TestCase):
    def test_returns_the_real_unwrapped_dek(self):
        tenant = _create_tenant(suffix="cold")
        row = mint_and_wrap_dek(tenant)

        dek = cache.get_dek(tenant.id, 0)

        expected = azure_client.unwrap_dek(tenant.id, bytes(row.wrapped_dek))
        self.assertEqual(dek, expected)
        self.assertEqual(len(dek), 32)

    def test_raises_when_no_dek_minted(self):
        tenant = _create_tenant(suffix="unminted")
        with self.assertRaises(TenantDek.DoesNotExist):
            cache.get_dek(tenant.id, 0)

    def test_raises_when_kek_purged_before_first_cache(self):
        tenant = _create_tenant(suffix="purged")
        mint_and_wrap_dek(tenant)
        azure_client.purge_kek(tenant.id)  # mock: removes registry entry outright

        with self.assertRaises(LookupError):
            cache.get_dek(tenant.id, 0)


class GetDekCacheHitTest(_ForceMockAzureMixin, TestCase):
    def test_second_call_does_zero_unwraps(self):
        tenant = _create_tenant(suffix="hit")
        mint_and_wrap_dek(tenant)

        first = cache.get_dek(tenant.id, 0)

        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            second = cache.get_dek(tenant.id, 0)

        mock_unwrap.assert_not_called()
        self.assertEqual(first, second)

    def test_survives_simulated_kv_outage_after_caching(self):
        tenant = _create_tenant(suffix="outage")
        mint_and_wrap_dek(tenant)

        warm = cache.get_dek(tenant.id, 0)  # populates the cache

        with patch(
            "apps.crypto.cache.azure_client.unwrap_dek",
            side_effect=RuntimeError("Key Vault is down"),
        ):
            # A cache HIT must never touch the (now-broken) broker call.
            served = cache.get_dek(tenant.id, 0)

        self.assertEqual(served, warm)


class PrimeTest(_ForceMockAzureMixin, TestCase):
    def test_prime_populates_without_touching_broker(self):
        fake_dek = b"P" * 32
        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            cache.prime("fake-tenant-never-in-db", 0, fake_dek)
            served = cache.get_dek("fake-tenant-never-in-db", 0)

        mock_unwrap.assert_not_called()
        self.assertEqual(served, fake_dek)

    def test_prime_is_visible_to_a_later_get_dek_call(self):
        cache.prime("prime-visibility-tenant", 5, b"Q" * 32)
        self.assertEqual(cache.get_dek("prime-visibility-tenant", 5), b"Q" * 32)


class ConcurrentColdMissTest(_ForceMockAzureMixin, TransactionTestCase):
    """Real threads need a real (committed) DB row visible from every
    connection — plain TestCase's per-test atomic wrapper is only visible on
    the main thread's connection, so worker threads would see no tenant/DEK
    row at all and every one would raise DoesNotExist. TransactionTestCase
    commits for real (and truncates after), which is what this test needs.
    """

    def test_concurrent_get_dek_calls_do_exactly_one_unwrap(self):
        tenant = _create_tenant(suffix="concurrent")
        mint_and_wrap_dek(tenant)

        results: list[bytes] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(cache.get_dek(tenant.id, 0))
            except BaseException as exc:  # noqa: BLE001 - surface any thread error to the test
                errors.append(exc)

        with patch(
            "apps.crypto.cache.azure_client.unwrap_dek",
            wraps=azure_client.unwrap_dek,
        ) as spy_unwrap:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == results[0] for r in results))
        spy_unwrap.assert_called_once()


class EvictTest(_ForceMockAzureMixin, TestCase):
    """`evict`/`evict_tenant` drop cached DEKs for a DELIBERATE re-key only —
    never as a side effect of a Key Vault failure."""

    def tearDown(self):
        cache._CACHE.clear()
        azure_client._MOCK_KEK_REGISTRY.clear()
        super().tearDown()

    def test_evict_removes_the_cached_entry(self):
        cache.prime("evict-one", 0, b"A" * 32)
        self.assertIn(("evict-one", 0), cache._CACHE)

        cache.evict("evict-one", 0)

        self.assertNotIn(("evict-one", 0), cache._CACHE)

    def test_evict_of_a_missing_entry_is_a_noop(self):
        cache.evict("evict-never-cached", 7)  # must not raise
        self.assertNotIn(("evict-never-cached", 7), cache._CACHE)

    def test_evict_tenant_drops_all_epochs_for_that_tenant_only(self):
        cache.prime("evict-multi", 0, b"A" * 32)
        cache.prime("evict-multi", 1, b"B" * 32)
        cache.prime("evict-other", 0, b"C" * 32)

        cache.evict_tenant("evict-multi")

        self.assertNotIn(("evict-multi", 0), cache._CACHE)
        self.assertNotIn(("evict-multi", 1), cache._CACHE)
        self.assertIn(("evict-other", 0), cache._CACHE)  # a different tenant is untouched

    def test_kv_unwrap_failure_never_evicts_a_warm_entry(self):
        """A broker failure resolving one tenant must not disturb another's
        already-cached DEK — eviction is re-key-only, never an outage hook."""
        warm = _create_tenant(suffix="warm")
        mint_and_wrap_dek(warm)
        warm_dek = cache.get_dek(warm.id, 0)  # populate the cache

        cold = _create_tenant(suffix="cold-fail")
        mint_and_wrap_dek(cold)

        with (
            patch(
                "apps.crypto.cache.azure_client.unwrap_dek",
                side_effect=RuntimeError("Key Vault down"),
            ),
            self.assertRaises(RuntimeError),
        ):
            cache.get_dek(cold.id, 0)

        # `warm` is still served from cache, unchanged, with zero broker calls.
        self.assertIn((str(warm.id), 0), cache._CACHE)
        with patch("apps.crypto.cache.azure_client.unwrap_dek") as mock_unwrap:
            still_warm = cache.get_dek(warm.id, 0)
        mock_unwrap.assert_not_called()
        self.assertEqual(still_warm, warm_dek)


class FreshStartEvictsCacheTest(_ForceMockAzureMixin, TestCase):
    """End-to-end: after a fresh-start re-key (purged KEK -> new epoch-0 DEK),
    the re-keying process must seal new content under the NEW DEK — not the
    stale one it had cached — so any other process can read it back."""

    def tearDown(self):
        cache._CACHE.clear()
        azure_client._MOCK_KEK_REGISTRY.clear()
        super().tearDown()

    def test_fresh_start_evicts_stale_dek_so_new_ciphertext_round_trips(self):
        tenant = _create_tenant(suffix="rekey")
        mint_and_wrap_dek(tenant)

        # Warm THIS process's cache with the OLD DEK by encrypting under it.
        blob_old = box.encrypt(tenant.id, "appchatmessage", "user_text", "before re-key")
        self.assertIn((str(tenant.id), 0), cache._CACHE)

        # Deprovision + post-grace purge, then re-provision -> fresh-start branch.
        azure_client.begin_delete_kek(tenant.id)
        azure_client.purge_kek(tenant.id)
        mint_and_wrap_dek(tenant)

        # The stale DEK is gone from this process's cache (the fix).
        self.assertNotIn((str(tenant.id), 0), cache._CACHE)

        # New writes seal under the NEW DEK: encrypt, then decrypt from a COLD
        # cache (standing in for any OTHER process) resolves the CURRENT row and
        # reads it back cleanly. Without the evict, box.encrypt would seal under
        # the stale cached DEK and this cold-cache read would fail closed.
        blob_new = box.encrypt(tenant.id, "appchatmessage", "user_text", "after re-key")
        cache._CACHE.clear()
        self.assertEqual(
            box.decrypt(tenant.id, "appchatmessage", "user_text", blob_new).reveal(),
            "after re-key",
        )

        # The pre-re-key ciphertext is unrecoverable — crypto-shred by design.
        cache._CACHE.clear()
        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, "appchatmessage", "user_text", blob_old)
