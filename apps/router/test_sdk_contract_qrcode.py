"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

from io import BytesIO

import qrcode
from django.test import SimpleTestCase


class QrCodeSdkContractTest(SimpleTestCase):
    def test_make_and_png_save_shape(self):
        image = qrcode.make("https://example.test/connect")
        output = BytesIO()
        image.save(output, format="PNG")

        self.assertTrue(output.getvalue().startswith(b"\x89PNG"))
