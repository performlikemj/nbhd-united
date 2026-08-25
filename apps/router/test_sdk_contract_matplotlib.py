"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from django.test import SimpleTestCase
from matplotlib.patches import FancyBboxPatch


class MatplotlibSdkContractTest(SimpleTestCase):
    def test_chart_objects_used_by_renderer_construct_offline(self):
        figure, axes = plt.subplots()
        axes.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        axes.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _position: f"{value:.0f}"))
        patch = FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.1")
        axes.add_patch(patch)

        self.assertEqual(len(axes.patches), 1)
        plt.close(figure)
