"""The basemap a map card is drawn on comes through Home Assistant's own tile proxy (app 0.4.24).

Nothing here touches the network: the websocket command and the tile request are both injected, so these tests
say exactly what the add-on asks for and how it behaves when Home Assistant cannot answer. The point of the
last class is the one thing that must never change: every request goes to the Home Assistant base, never to a
tile server.
"""
import asyncio
import importlib.util
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'screen_manager/app'))
import map_card  # noqa: E402
import map_tiles  # noqa: E402

HAS_PIL = importlib.util.find_spec('PIL') is not None
HAS_AIOHTTP = importlib.util.find_spec('aiohttp') is not None
if HAS_AIOHTTP:
    from server import HomeAssistant


def png(size=(map_tiles.TILE_PX, map_tiles.TILE_PX), colour=(120, 160, 90)):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', size, colour).save(out, 'PNG')
    return out.getvalue()


class Proxy:
    """Home Assistant as the tile source sees it: a websocket that hands out tokens and a proxy that serves PNGs."""

    def __init__(self, tokens=('token-1', 'token-2', 'token-3'), refuse=(), broken=(), fail_token=False):
        self.tokens, self.handed = list(tokens), []
        self.refuse, self.broken, self.fail_token = set(refuse), set(broken), fail_token
        self.asked, self.live, self.peak, self.tokens_asked = [], 0, 0, 0
        self.body = png() if HAS_PIL else b'not-a-png'

    async def token(self):
        self.tokens_asked += 1
        if self.fail_token:
            raise ConnectionError("Home Assistant isn't connected.")
        value = self.tokens[min(self.tokens_asked - 1, len(self.tokens) - 1)]
        self.handed.append(value)
        return value

    async def fetch(self, z, x, y, token):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(0.01)
            self.asked.append((z, x, y, token))
            if token in self.refuse:
                raise map_tiles.Unauthorized('the proxy refused this token')
            if (z, x, y) in self.broken:
                raise OSError('connection reset')
            return self.body
        finally:
            self.live -= 1


def source(proxy, now=1000.0):
    clock = {'now': now}
    tiles = map_tiles.TileSource(proxy.token, proxy.fetch, clock=lambda: clock['now'])
    return tiles, clock


def view(size=(320, 240), zoom='15', lat=52.0, lon=5.0):
    return map_card.viewport([(lat, lon)], [], zoom, size, map_card.MAX_TILES)


@unittest.skipUnless(HAS_PIL, 'Pillow comes with ESPHome in the add-on image')
class Tokens(unittest.IsolatedAsyncioTestCase):
    async def test_one_token_serves_every_tile_of_a_render(self):
        proxy = Proxy()
        tiles, _ = source(proxy)
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(proxy.tokens_asked, 1)
        self.assertGreater(len(proxy.asked), 1)
        self.assertEqual({token for *_, token in proxy.asked}, {'token-1'})

    async def test_a_refused_token_is_refreshed_once_and_the_tiles_asked_again(self):
        proxy = Proxy(refuse={'token-1'})
        tiles, _ = source(proxy)
        image = await tiles.image(view(), (320, 240))
        self.assertIsNotNone(image)
        self.assertEqual(proxy.tokens_asked, 2)
        self.assertEqual(proxy.handed, ['token-1', 'token-2'])
        self.assertTrue(tiles.available())

    async def test_a_token_refused_twice_gives_up_on_this_render(self):
        proxy = Proxy(refuse={'token-1', 'token-2'})
        tiles, _ = source(proxy)
        self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(proxy.tokens_asked, 2)

    async def test_a_token_older_than_the_refresh_is_replaced_before_it_fails(self):
        proxy = Proxy()
        tiles, clock = source(proxy)
        self.assertEqual(await tiles.token(), 'token-1')
        clock['now'] += map_tiles.TOKEN_REFRESH - 1
        self.assertEqual(await tiles.token(), 'token-1')
        clock['now'] += 2
        self.assertEqual(await tiles.token(), 'token-2')
        self.assertEqual(proxy.tokens_asked, 2)

    async def test_the_refresh_stays_under_home_assistants_own_rotation(self):
        # Home Assistant rotates the token every 30 minutes (map_tiles.const.TOKEN_CHANGE_INTERVAL).
        self.assertLess(map_tiles.TOKEN_REFRESH, 30 * 60)

    async def test_a_token_that_is_not_a_plain_string_is_refused(self):
        for answer in (None, 42, {'nothing': 'here'}, ''):
            proxy = Proxy()
            proxy.token = lambda answer=answer: asyncio.sleep(0, result=answer)
            tiles, _ = source(proxy)
            with self.subTest(answer=answer):
                self.assertIsNone(await tiles.image(view(), (320, 240)))
                self.assertFalse(tiles.available())


@unittest.skipUnless(HAS_PIL, 'Pillow comes with ESPHome in the add-on image')
class Requests(unittest.IsolatedAsyncioTestCase):
    async def test_no_more_than_the_allowed_number_of_fetches_is_in_flight(self):
        proxy = Proxy()
        tiles, _ = source(proxy)
        # A frame that needs more tiles than the cap allows in flight at once.
        frame = view((1024, 640), '15')
        self.assertGreater(frame.tiles, map_tiles.MAX_CONCURRENT)
        self.assertIsNotNone(await tiles.image(frame, (1024, 640)))
        self.assertEqual(proxy.peak, map_tiles.MAX_CONCURRENT)
        self.assertEqual(proxy.live, 0)

    async def test_a_second_render_of_the_same_ground_asks_for_nothing(self):
        proxy = Proxy()
        tiles, _ = source(proxy)
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        asked = len(proxy.asked)
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(len(proxy.asked), asked)
        # A second frame size over the same ground reuses the same tiles too.
        self.assertIsNotNone(await tiles.image(view(), (160, 120)))

    async def test_a_tile_older_than_its_lifetime_is_fetched_again(self):
        proxy = Proxy()
        tiles, clock = source(proxy)
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        asked = len(proxy.asked)
        clock['now'] += map_tiles.TILE_TTL - 1
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(len(proxy.asked), asked)
        clock['now'] += 2
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(len(proxy.asked), asked * 2)

    async def test_the_lifetime_matches_home_assistants_own_cache(self):
        self.assertEqual(map_tiles.TILE_TTL, 7 * 24 * 60 * 60)

    async def test_the_cache_drops_the_least_recently_used_tile_and_never_grows_past_its_bound(self):
        proxy = Proxy()
        tiles, _ = source(proxy)
        original = map_tiles.CACHE_BYTES
        try:
            map_tiles.CACHE_BYTES = len(proxy.body) * 3 + 1
            first = [(15, 100, 100), (15, 101, 100), (15, 102, 100)]
            for key in first:
                self.assertIsNotNone(await tiles.fetch(*key))
            self.assertLessEqual(tiles.bytes_used, map_tiles.CACHE_BYTES)
            # Touching the first one makes the second the least recently used.
            self.assertIsNotNone(await tiles.fetch(*first[0]))
            asked = len(proxy.asked)
            self.assertIsNotNone(await tiles.fetch(15, 103, 100))
            self.assertEqual(len(proxy.asked), asked + 1)
            self.assertLessEqual(tiles.bytes_used, map_tiles.CACHE_BYTES)
            self.assertNotIn(first[1], tiles.cached_keys())
            self.assertIn(first[0], tiles.cached_keys())
        finally:
            map_tiles.CACHE_BYTES = original

    async def test_a_frame_that_needs_more_tiles_than_the_budget_is_refused_instead_of_flooded(self):
        proxy = Proxy()
        tiles, _ = source(proxy)
        wide = map_card.Viewport(52.0, 5.0, 15, (2048, 2048))
        self.assertGreater(wide.tiles, map_tiles.MAX_TILES)
        self.assertIsNone(await tiles.image(wide, (2048, 2048)))
        self.assertEqual(proxy.asked, [])
        self.assertEqual(proxy.tokens_asked, 0)

    async def test_the_budget_is_the_one_the_geometry_plans_with(self):
        self.assertEqual(map_tiles.MAX_TILES, map_card.MAX_TILES)
        self.assertEqual(map_tiles.TILE_PX, map_card.TILE_PX)

    async def test_tiles_that_fail_leave_their_place_empty_and_the_card_is_still_drawn(self):
        frame = view((320, 240), '15')
        keys = frame.tile_keys()
        proxy = Proxy(broken={keys[0]})
        tiles, _ = source(proxy)
        image = await tiles.image(frame, (320, 240))
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (320, 240))
        self.assertEqual(image.mode, 'RGBA')
        # Where the tile failed the basemap is see-through, so the render's own background shows there.
        self.assertIn(0, image.getchannel('A').getdata())
        self.assertIn(255, image.getchannel('A').getdata())
        self.assertTrue(tiles.available())

    async def test_not_one_tile_means_no_basemap_at_all(self):
        frame = view((320, 240), '15')
        proxy = Proxy(broken=set(frame.tile_keys()))
        tiles, _ = source(proxy)
        self.assertIsNone(await tiles.image(frame, (320, 240)))

    async def test_a_tile_that_is_not_a_picture_is_left_out(self):
        proxy = Proxy()
        proxy.body = b'<html>no tiles here</html>'
        tiles, _ = source(proxy)
        self.assertIsNone(await tiles.image(view(), (320, 240)))

    async def test_a_tile_that_cannot_be_read_is_not_kept_for_a_week(self):
        """A proxy that once answered a page of HTML must not leave the card without streets until the cache expires.

        What cannot be drawn is dropped, so the next render asks for it again and the card comes back by itself."""
        proxy = Proxy()
        proxy.body = b'<html>no tiles here</html>'
        tiles, _ = source(proxy)
        self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(tiles.cached_keys(), [])
        self.assertEqual(tiles.bytes_used, 0)
        proxy.body = png()
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))

    async def test_an_incomplete_basemap_says_how_much_of_it_is_missing(self):
        """A render made from half a mosaic is provisional: the caller may draw it, but must not keep it."""
        frame = view((320, 240), '15')
        keys = frame.tile_keys()
        proxy = Proxy(broken={keys[0]})
        tiles, _ = source(proxy)
        image = await tiles.image(frame, (320, 240))
        self.assertEqual(image.info.get('map_missing'), 1)
        # The tiles that failed were never cached, so the next render asks only for those.
        self.assertNotIn(keys[0], tiles.cached_keys())
        asked = len(proxy.asked)
        proxy.broken = set()
        whole = await tiles.image(frame, (320, 240))
        self.assertEqual(len(proxy.asked), asked + 1)
        self.assertEqual(whole.info.get('map_missing'), 0)


@unittest.skipUnless(HAS_PIL, 'Pillow comes with ESPHome in the add-on image')
class WhenHomeAssistantCannot(unittest.IsolatedAsyncioTestCase):
    async def test_a_home_assistant_without_the_integration_is_left_alone_for_a_while(self):
        proxy = Proxy(fail_token=True)
        tiles, clock = source(proxy)
        self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertFalse(tiles.available())
        self.assertEqual(proxy.tokens_asked, 1)
        # The next renders ask nothing at all.
        for _ in range(3):
            self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(proxy.tokens_asked, 1)
        clock['now'] += map_tiles.UNAVAILABLE_COOLDOWN - 1
        self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(proxy.tokens_asked, 1)
        clock['now'] += 2
        self.assertTrue(tiles.available())
        proxy.fail_token = False
        self.assertIsNotNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(proxy.tokens_asked, 2)

    async def test_the_cooldown_is_long_enough_to_be_quiet(self):
        self.assertGreaterEqual(map_tiles.UNAVAILABLE_COOLDOWN, 300)

    async def test_it_says_so_once_and_again_when_the_basemap_comes_back(self):
        proxy = Proxy(fail_token=True)
        tiles, clock = source(proxy)
        with self.assertLogs('map_tiles', level='INFO') as logs:
            self.assertIsNone(await tiles.image(view(), (320, 240)))
            clock['now'] += map_tiles.UNAVAILABLE_COOLDOWN + 1
            self.assertIsNone(await tiles.image(view(), (320, 240)))
        self.assertEqual(len([line for line in logs.output if 'no map tiles' in line]), 1, logs.output)


@unittest.skipUnless(HAS_AIOHTTP, 'aiohttp comes with the add-on image')
class OnlyThroughHomeAssistant(unittest.IsolatedAsyncioTestCase):
    """The tile requests themselves: where they go and what they carry."""

    def session(self, status=200, body=b'tile'):
        calls = []

        class Response:
            content_length = len(body)

            def __init__(self):
                self.status = status

            class content:
                @staticmethod
                async def iter_chunked(size):
                    yield body

            def raise_for_status(self):
                if self.status >= 400:
                    raise OSError(f'status {self.status}')

            async def __aenter__(self):
                return self

            async def __aexit__(self, *error):
                return False

        class Session:
            def get(self, url, headers=None, timeout=None, allow_redirects=True):
                calls.append((url, (headers or {}).get('Authorization'), allow_redirects))
                return Response()
        return Session(), calls

    async def test_a_tile_is_asked_of_home_assistant_and_of_nobody_else(self):
        session, calls = self.session()
        ha = HomeAssistant(session, 'http://supervisor/core/api', 'secret')
        self.assertEqual(await ha.map_tile(15, 16824, 10770, 'abc123'), b'tile')
        url, authorization, redirects = calls[0]
        self.assertEqual(url, 'http://supervisor/core/api/map_tiles/raster/15/16824/10770.png?token=abc123')
        self.assertEqual(authorization, 'Bearer secret')
        self.assertFalse(redirects)
        self.assertNotIn('openstreetmap', url)
        self.assertTrue(url.startswith('http://supervisor/core/api' + map_tiles.PATH + '/'))

    async def test_every_tile_of_a_whole_render_stays_on_the_home_assistant_base(self):
        session, calls = self.session(body=png() if HAS_PIL else b'tile')
        ha = HomeAssistant(session, 'http://ha.example/api', 'secret')
        tiles = map_tiles.TileSource(lambda: asyncio.sleep(0, result='tok'), ha.map_tile)
        await tiles.image(view((480, 320), '15'), (480, 320))
        self.assertTrue(calls)
        for url, _, _ in calls:
            self.assertTrue(url.startswith('http://ha.example/api/map_tiles/raster/'), url)
            self.assertNotIn('openstreetmap.org', url)
            self.assertNotIn('tile.openstreetmap', url)

    async def test_a_tile_request_carries_no_entity_and_no_name(self):
        session, calls = self.session()
        ha = HomeAssistant(session, 'http://ha/api', 'secret')
        await ha.map_tile(15, 1, 2, 'abc')
        self.assertNotIn('person', calls[0][0])
        self.assertNotIn('device_tracker', calls[0][0])
        # A zoom and two whole numbers, and the token Home Assistant handed out: nothing else.
        self.assertRegex(calls[0][0], r'/map_tiles/raster/\d+/\d+/\d+\.png\?token=[A-Za-z0-9_-]+$')

    async def test_a_tile_outside_the_map_is_never_asked_for(self):
        session, calls = self.session()
        ha = HomeAssistant(session, 'http://ha/api', 'secret')
        for z, x, y in ((20, 1, 1), (-1, 0, 0), (2, 4, 0), (2, 0, 4), (2, -1, 0), (2.5, 1, 1), (True, 1, 1)):
            with self.subTest(tile=(z, x, y)), self.assertRaises(ValueError):
                await ha.map_tile(z, x, y, 'abc')
        self.assertEqual(calls, [])

    async def test_a_token_that_could_steer_the_address_is_refused(self):
        session, calls = self.session()
        ha = HomeAssistant(session, 'http://ha/api', 'secret')
        for token in ('../../../secret', 'a&b=c', 'a b', '', 'x' * 500, None, 12):
            with self.subTest(token=token), self.assertRaises(ValueError):
                await ha.map_tile(15, 1, 1, token)
        self.assertEqual(calls, [])

    async def test_a_refusal_of_the_token_is_told_apart_from_any_other_failure(self):
        for status in (401, 403):
            session, _ = self.session(status=status)
            ha = HomeAssistant(session, 'http://ha/api', 'secret')
            with self.subTest(status=status), self.assertRaises(map_tiles.Unauthorized):
                await ha.map_tile(15, 1, 1, 'abc')
        session, _ = self.session(status=500)
        ha = HomeAssistant(session, 'http://ha/api', 'secret')
        with self.assertRaises(OSError):
            await ha.map_tile(15, 1, 1, 'abc')

    async def test_a_tile_larger_than_a_tile_could_be_is_dropped(self):
        session, _ = self.session(body=b'x' * (map_tiles.MAX_TILE_BYTES + 1))
        ha = HomeAssistant(session, 'http://ha/api', 'secret')
        with self.assertRaises(ValueError):
            await ha.map_tile(15, 1, 1, 'abc')

    async def test_the_token_comes_over_the_websocket_the_app_already_has(self):
        ha = HomeAssistant(None, 'http://ha/api', 'secret')
        asked = []

        async def request(kind, **data):
            asked.append((kind, data))
            return answer

        ha.request = request
        # E1: Home Assistant returns the token itself; an object wrapping it is taken too.
        for answer in ('plain-token', {'token': 'plain-token'}):
            self.assertEqual(await ha.map_tiles_token(), 'plain-token')
        self.assertEqual([kind for kind, _ in asked], ['map_tiles/access_token'] * 2)
        self.assertEqual(asked[0][1], {})
        for answer in (None, 42, {}, ''):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                await ha.map_tiles_token()
