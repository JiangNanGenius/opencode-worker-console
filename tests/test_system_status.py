import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import system_status


class SystemStatusTests(unittest.TestCase):
    def setUp(self):
        system_status._CACHE.update(at=0.0, value=None)

    def test_snapshot_returns_bounded_numeric_host_metrics_and_uses_cache(self):
        disk = type('Disk', (), {'total': 1000, 'used': 700, 'free': 300})()
        with patch.object(system_status, '_cpu_percent', return_value=42.5) as cpu, \
                patch.object(system_status, '_memory', return_value={
                    'total_bytes': 2000, 'available_bytes': 500, 'used_percent': 75.0}), \
                patch.object(system_status.shutil, 'disk_usage', return_value=disk), \
                patch.object(system_status.os, 'getloadavg', return_value=(1.0, .5, .25)):
            first = system_status.snapshot(now=100)
            second = system_status.snapshot(now=103)
        self.assertEqual(first['cpu_percent'], 42.5)
        self.assertEqual(first['disk']['free_bytes'], 300)
        self.assertEqual(first['memory']['used_percent'], 75.0)
        self.assertEqual(first['load_average'], [1.0, .5, .25])
        self.assertEqual(second, first)
        cpu.assert_called_once()


if __name__ == '__main__':
    unittest.main()
