import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import holiday_calendar


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return json.dumps(self.payload).encode()


class HolidayCalendarTests(unittest.TestCase):
    def test_refresh_normalizes_subscription_and_skips_bad_records(self):
        payload = {'Years': {'2026': [
            {'StartDate': '2026-10-01', 'EndDate': '2026-10-03',
             'URL': 'https://www.gov.cn/example'},
            {'StartDate': None, 'EndDate': 'broken'},
        ]}, 'Generated': 'test'}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(holiday_calendar, 'CACHE', Path(td) / 'calendar.json'), \
                patch.object(holiday_calendar, 'config', return_value={
                    'deepseek_holiday_calendar': {'enabled': True,
                        'urls': ['https://calendar.example/holidays.json'],
                        'refresh_hours': 24}}), \
                patch.object(holiday_calendar.urllib.request, 'urlopen',
                             return_value=_Response(payload)):
            result = holiday_calendar.refresh(force=True, now=123)
            self.assertEqual(result['holidays'], [
                '2026-10-01', '2026-10-02', '2026-10-03'])
            self.assertEqual(result['subscription_url'],
                             'https://calendar.example/holidays.json')
            self.assertEqual(result['authority_urls'], ['https://www.gov.cn/example'])
            self.assertTrue((Path(td) / 'calendar.json').exists())

    def test_failed_refresh_keeps_last_good_cache(self):
        cached = {'holidays': ['2026-01-01'], 'fetched_at': 10,
                  'subscription_url': 'https://calendar.example/holidays.json'}
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / 'calendar.json'
            cache.write_text(json.dumps(cached))
            with patch.object(holiday_calendar, 'CACHE', cache), \
                    patch.object(holiday_calendar, 'config', return_value={
                        'deepseek_holiday_calendar': {'enabled': True,
                            'urls': ['https://calendar.example/holidays.json'],
                            'refresh_hours': 24}}), \
                    patch.object(holiday_calendar.urllib.request, 'urlopen',
                                 side_effect=OSError('offline')):
                self.assertEqual(holiday_calendar.refresh(force=True, now=456), cached)


if __name__ == '__main__':
    unittest.main()
