from datetime import datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server as app
import weather


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DATA = Path(self.temp.name)
        app.initialize()
        with app.DRINK_WEATHER_CACHE_LOCK:
            app.DRINK_WEATHER_CACHE.update(key=None, expires=0, forecasts=None, available=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_weather_preference_thresholds_and_trend(self):
        self.assertEqual(weather.temperature_preference(27.1, 24), 'cold')
        self.assertEqual(weather.temperature_preference(17.9, 21), 'hot')
        self.assertEqual(weather.temperature_preference(24, 21), 'cold')
        self.assertEqual(weather.temperature_preference(21, 24), 'hot')
        self.assertEqual(weather.temperature_preference(24, 22), 'random')
        self.assertEqual(weather.temperature_preference(24), 'random')
        self.assertEqual(weather.temperature_preference(27, 24), 'cold')
        self.assertEqual(weather.temperature_preference(18, 21), 'hot')

    def test_qweather_api_host_validation(self):
        self.assertEqual(weather.validate_api_host('a1.xy.qweatherapi.com'), 'a1.xy.qweatherapi.com')
        self.assertEqual(weather.validate_api_host('api.qweather.com'), 'api.qweather.com')
        for host in ('https://qweatherapi.com/path', 'attacker.example', 'qweatherapi.com:443', ''):
            with self.subTest(host=host), self.assertRaises(ValueError):
                weather.validate_api_host(host)

    def test_parse_daily_forecast_uses_hangzhou_local_date_and_maximum(self):
        result = weather.parse_daily_forecast({'days': [
            {'forecastStartTime': '2026-09-27T00:00+08:00',
             'temperatureMax': {'value': 28.5}},
            {'forecastStartTime': '2026-09-28T00:00+08:00',
             'temperatureMax': {'value': 25}},
        ]})
        self.assertEqual(result, [
            {'date': '2026-09-27', 'max_temp': 28.5},
            {'date': '2026-09-28', 'max_temp': 25.0},
        ])

    def test_qweather_request_keeps_key_in_header_and_reads_daily_forecast(self):
        payload = {'days': [{'forecastStartTime': '2026-09-27T00:00+08:00',
                             'temperatureMax': {'value': 29}}]}

        class Response:
            status = 200
            headers = {'Content-Encoding': 'gzip'}

            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1): return gzip.compress(json.dumps(payload).encode())

        with patch.object(weather.request, 'urlopen', return_value=Response()) as open_url:
            result = weather.fetch_hangzhou_forecast('host.xy.qweatherapi.com', 'sample-key')
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header('X-qw-api-key'), 'sample-key')
        self.assertIn('/weather/v1/daily/30.27/120.15', request.full_url)
        self.assertEqual(result[0]['max_temp'], 29)

    def test_weather_settings_keep_secret_server_side_and_weather_falls_back_unconfigured(self):
        self.assertEqual(app.api('GET', '/knowledge/api/drink-weather', {}, {}),
                         {'available': False, 'reason': 'not_configured'})
        with self.assertRaises(app.Problem) as ctx:
            app.api('PUT', '/knowledge/api/drink-weather-settings',
                    {'api_host': 'attacker.example', 'api_key': 'key'}, {})
        self.assertEqual(ctx.exception.status, 400)

        saved = app.api('PUT', '/knowledge/api/drink-weather-settings', {
            'api_host': 'host.xy.qweatherapi.com', 'api_key': 'sample-key'}, {})
        self.assertEqual(saved, {'api_host': 'host.xy.qweatherapi.com', 'has_key': True})
        self.assertEqual(app.api('GET', '/knowledge/api/drink-weather-settings', {}, {}), saved)
        app.api('PUT', '/knowledge/api/drink-weather-settings', {
            'api_host': 'host.xy.qweatherapi.com', 'api_key': ''}, {})
        with app.db() as c:
            self.assertEqual(app.drink_weather_settings(c)['api_key'], 'sample-key')

    def test_weather_snapshot_uses_saved_yesterday_forecast(self):
        today = datetime.now(weather.HANGZHOU_TZ).date()
        yesterday = (today - app.timedelta(days=1)).isoformat()
        today_key = today.isoformat()
        with app.db() as c:
            c.execute('INSERT INTO drink_weather_daily VALUES(?,?,?)',
                      (yesterday, 20.0, app.now()))
        with patch.object(weather, 'fetch_hangzhou_forecast', return_value=[
                {'date': today_key, 'max_temp': 24.0}]), \
                patch.object(app, 'drink_weather_settings', return_value={
                    'api_host': 'host.xy.qweatherapi.com', 'api_key': 'sample-key'}):
            snapshot = app.drink_weather_snapshot(force_refresh=True)
        self.assertTrue(snapshot['available'])
        self.assertEqual(snapshot['yesterday_max'], 20.0)
        self.assertEqual(snapshot['temperature_preference'], 'cold')


if __name__ == '__main__':
    unittest.main()
