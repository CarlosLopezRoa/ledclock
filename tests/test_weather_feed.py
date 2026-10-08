"""The weather feed must never trade fresh data for older data.

These exist because of one evening: the Mac woke, the first fetch failed
before the network was back, and the fallback served a disk cache written
thirteen days earlier — keyed on a jittering coordinate that happened to match
it. That replaced an hour-old forecast in memory, the trend window no longer
overlapped the data, and the menu bar graph went flat until the next fetch.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ledclock import daemon
from ledclock.sources import weather as weather_source


def _weather(age_seconds: float, *, stale: bool = False) -> weather_source.Weather:
    return weather_source.Weather(
        current=None, days=[], fetched_at=time.time() - age_seconds, stale=stale
    )


def _feed() -> daemon.WeatherFeed:
    config = SimpleNamespace(
        weather=SimpleNamespace(
            resolve_coordinates=lambda: (43.0, 3.0, "test"),
            unit="celsius",
            refresh_seconds=600,
        )
    )
    return daemon.WeatherFeed(config)


class TestNeverTradeDown(unittest.IsolatedAsyncioTestCase):
    async def test_an_older_cache_does_not_replace_newer_data(self) -> None:
        feed = _feed()
        feed.current = _weather(3600)
        ancient = _weather(13 * 24 * 3600, stale=True)
        with mock.patch.object(weather_source, "fetch", mock.AsyncMock(return_value=ancient)):
            fresh = await feed.refresh()
        self.assertFalse(fresh)
        self.assertLess(feed.current.age, 2 * 3600)

    async def test_newer_data_does_replace(self) -> None:
        feed = _feed()
        feed.current = _weather(3600)
        newer = _weather(5)
        with mock.patch.object(weather_source, "fetch", mock.AsyncMock(return_value=newer)):
            fresh = await feed.refresh()
        self.assertTrue(fresh)
        self.assertIs(feed.current, newer)

    async def test_a_stale_cache_is_still_better_than_nothing(self) -> None:
        feed = _feed()
        cached = _weather(7200, stale=True)
        with mock.patch.object(weather_source, "fetch", mock.AsyncMock(return_value=cached)):
            fresh = await feed.refresh()
        self.assertFalse(fresh, "stale data should prompt a quick retry")
        self.assertIs(feed.current, cached)

    async def test_a_failure_keeps_what_we_had(self) -> None:
        feed = _feed()
        held = _weather(600)
        feed.current = held
        boom = mock.AsyncMock(side_effect=OSError(8, "nodename nor servname provided"))
        with mock.patch.object(weather_source, "fetch", boom):
            fresh = await feed.refresh()
        self.assertFalse(fresh)
        self.assertIs(feed.current, held)


class TestCacheKey(unittest.TestCase):
    def test_location_jitter_stays_in_one_file(self) -> None:
        """Wi-Fi geolocation wanders by tens of metres between updates.

        The spread is real — three consecutive fixes taken without moving —
        but it is planted on an invented spot in the Atlantic, because five
        decimals of a real one is a doorstep and this file is public. At four
        decimals these were three separate cache files; weather does not vary
        at that scale, so they should be one.
        """
        seen = [(45.00118, -20.00097), (45.00123, -20.00121), (45.00106, -20.00081)]
        paths = {weather_source._cache_path(lat, lon, "celsius") for lat, lon in seen}
        self.assertEqual(len(paths), 1, paths)
