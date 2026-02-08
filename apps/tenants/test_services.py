"""Additional tenant service coverage."""
from django.test import TestCase

from .services import create_tenant


class TenantServiceTest(TestCase):
    def test_duplicate_chat_id_raises_value_error(self):
        create_tenant(display_name="First", telegram_chat_id=1001)

        with self.assertRaises(ValueError):
            create_tenant(display_name="Second", telegram_chat_id=1001)
