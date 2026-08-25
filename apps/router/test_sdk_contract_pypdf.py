"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from django.test import SimpleTestCase
from pypdf import PdfReader
from pypdf._page import PageObject


class PyPdfSdkContractTest(SimpleTestCase):
    def test_reader_and_page_text_extraction_shapes_exist(self):
        inspect.signature(PdfReader).bind("document.pdf")
        page = PageObject.create_blank_page(width=100, height=100)
        inspect.signature(PageObject.extract_text).bind(page, extraction_mode="layout")

        self.assertTrue(hasattr(PdfReader, "pages"))
