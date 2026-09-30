"""A map card reaches a screen the way a camera does (app 0.4.24, firmware 0.15.0).

The screen asks for the pictures of its page, the app looks every named tile up in that screen's **saved** layout,
renders the map ones itself and puts them in the same atlas the cameras go in. Nothing here touches the network: the
basemap source is injected, and these tests are as much about what never happens (a tile request for a card with no
basemap, an entity a screen named itself) as about the picture that comes out.
"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'screen_manager/app'))
sys.path.insert(0, str(ROOT / 'tests'))

import camera_feed  # noqa: E402
import map_card  # noqa: E402
import map_tiles  # noqa: E402
from core import validate_layout  # noqa: E402
from manager_fixtures import seed_layout, with_screen_grid  # noqa: E402
from test_camera import fake_ha  # noqa: E402

HAS_PIL = importlib.util.find_spec('PIL') is not None
HAS_AIOHTTP = importlib.util.find_spec('aiohttp') is not None
if HAS_AIOHTTP:
    from server import Manager  # noqa: E402

MAP_FIRMWARE = '0.15.0'
HOME = {'state': 'zoning', 'attributes': {'friendly_name': 'Home', 'latitude': 52.0, 'longitude': 5.0, 'radius': 100}}


def person(lat=None, lon=None, state='home', name='Robin'):
    attributes = {'friendly_name': name}
    if lat is not None:
        attributes.update({'latitude': lat, 'longitude': lon})
    return {'state': state, 'attributes': attributes}


class Source:
    """The basemap as the renderer sees it: a counted stand-in for map_tiles.TileSource, with no network at all."""

    def __init__(self, colour=(210, 220, 200), ok=True):
        self.colour, self.ok, self.asked = colour, ok, []

    def available(self):
        return self.ok

    async def image(self, view, size):
        self.asked.append((view.tile_keys(), tuple(size)))
        if not self.ok:
            return None
        from PIL import Image
        return Image.new('RGBA', (int(size[0]), int(size[1])), self.colour + (255,))


def ready(ha, firmware=MAP_FIRMWARE):
    ha.states['sensor.d1_fw']['state'] = firmware
    ha.states['sensor.d3_fw']['state'] = firmware
    ha.states.update({'zone.home': dict(HOME), 'person.robin': person(52.001, 5.002),
                      'person.sam': person(52.004, 5.006, name='Sam'),
                      'device_tracker.phone': person(52.002, 5.001, name='Phone')})
    return ha


def tiles_of(*options):
    return validate_layout({'title': 'Hall', 'tiles': [{'entity': 'person.robin', 'name': 'Robin', 'options': dict(o)}
                                                       for o in options]})


def request(atlas, tiles='person.robin', dark='0', inbox='text.d1_tiles'):
    return {'inbox': inbox, 'tiles': tiles, 'size': '54', 'bg': ','.join(['FFFFFF'] * len(tiles.split(','))),
            'dark': dark, 'atlas': json.dumps(atlas)}


def sent(ha):
    return [entry[2] for entry in ha.log if entry[0] == 'send']


@unittest.skipUnless(HAS_AIOHTTP and HAS_PIL, 'needs aiohttp and Pillow')
class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        camera_feed.base_url.__defaults__[0].clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def manager(self, ha, layout, source=None):
        m = Manager(with_screen_grid(ha), Path(self.tmp.name) / 'screens.json')
        seed_layout(m, 'text.d1_tiles', layout)
        m.basemap = source if source is not None else Source()
        return m

    async def served(self, m, ha):
        from PIL import Image
        message = sent(ha)[-1]
        self.assertTrue(message['u'], 'no picture')
        token = message['u'].rsplit('/', 1)[1][:-4]
        status, raw, tag = await m.camera.serve(token)
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(raw)) as image:
            return image.convert('RGB'), tag

    async def test_a_map_tile_of_the_saved_layout_gets_a_picture_of_its_own_frame(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map', 'size': 'square'}))
        await m.answer_camera(request([[10, 12, 220, 200, 16, 0]]))
        image, _ = await self.served(m, ha)
        self.assertEqual(image.size, (230, 212))
        # The frame really carries the map, not the empty atlas background.
        self.assertNotEqual(image.getpixel((120, 100)), image.getpixel((2, 2)))

    async def test_a_screen_cannot_name_an_entity_it_has_no_map_tile_for(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map'}))
        ha.log.clear()
        with self.assertLogs('screen_manager', 'INFO') as logs:
            await m.answer_camera(request([[0, 0, 200, 200, 0, 0]], tiles='person.sam'))
        self.assertEqual(sent(ha), [])
        self.assertTrue(any('not the pictured tiles' in line for line in logs.output), logs.output)

    async def test_what_is_drawn_comes_from_the_saved_layout_and_never_from_the_request(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map', 'map': ['person.sam']}))
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        plain, _ = await self.served(m, ha)
        # The same request with extra fields a screen might invent changes nothing about who is drawn.
        ha.log.clear()
        await m.answer_camera({**request([[0, 0, 240, 200, 0, 0]]), 'map': 'device_tracker.phone',
                               'zoom': '11', 'basemap': 'none', 'labels': 'nothing'})
        again, _ = await self.served(m, ha)
        self.assertEqual(plain.tobytes(), again.tobytes())
        # Saving the companion away really does change the picture, so the check above means something.
        seed_layout(m, 'text.d1_tiles', tiles_of({'display': 'map'}))
        ha.log.clear()
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        alone, _ = await self.served(m, ha)
        self.assertNotEqual(plain.tobytes(), alone.tobytes())

    async def test_two_maps_of_the_same_person_with_other_companions_are_two_pictures(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map', 'map': ['person.sam']}))
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        one, first = await self.served(m, ha)
        seed_layout(m, 'text.d1_tiles', tiles_of({'display': 'map', 'map': ['device_tracker.phone']}))
        ha.log.clear()
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        two, second = await self.served(m, ha)
        self.assertNotEqual(one.tobytes(), two.tobytes())
        self.assertNotEqual(first, second, 'the strip cache must not answer with the other tile card')

    async def test_another_frame_and_another_look_are_another_picture(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map'}))
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        small, small_tag = await self.served(m, ha)
        ha.log.clear()
        await m.answer_camera(request([[0, 0, 300, 240, 0, 0]]))
        big, big_tag = await self.served(m, ha)
        self.assertEqual((small.size, big.size), ((240, 200), (300, 240)))
        self.assertNotEqual(small_tag, big_tag)
        ha.log.clear()
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]], dark='1'))
        night, night_tag = await self.served(m, ha)
        self.assertNotEqual(small.tobytes(), night.tobytes())
        self.assertNotEqual(small_tag, night_tag)

    async def test_a_map_has_no_pace_so_nothing_is_fetched_on_a_clock(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map'}))
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        token = sent(ha)[-1]['u'].rsplit('/', 1)[1][:-4]
        self.assertEqual(m.camera.links[token].live[3], [0])

    async def test_reloading_the_same_link_draws_where_people_are_now(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map', 'zoom': '15'}))
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        before, _ = await self.served(m, ha)
        ha.states['person.robin'] = person(52.02, 5.03)
        after, _ = await self.served(m, ha)
        self.assertNotEqual(before.tobytes(), after.tobytes())

    async def test_a_card_without_a_basemap_never_asks_for_a_tile(self):
        ha = ready(fake_ha())
        source = Source()
        m = self.manager(ha, tiles_of({'display': 'map', 'basemap': 'none'}), source)
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        image, _ = await self.served(m, ha)
        self.assertEqual(image.size, (240, 200))
        self.assertEqual(source.asked, [])

    async def test_a_basemap_that_cannot_be_had_still_gives_a_card(self):
        ha = ready(fake_ha())
        source = Source(ok=False)
        m = self.manager(ha, tiles_of({'display': 'map'}), source)
        await m.answer_camera(request([[0, 0, 240, 200, 0, 0]]))
        image, _ = await self.served(m, ha)
        self.assertEqual(image.size, (240, 200))
        self.assertTrue(source.asked, 'it does try once')

    async def test_a_board_without_pictures_and_a_screen_below_the_gate_get_nothing(self):
        ha = ready(fake_ha())
        ha.states['sensor.d3_fw']['state'] = '0.14.0'
        m = Manager(with_screen_grid(ha), Path(self.tmp.name) / 'screens.json')
        m.basemap = Source()
        for inbox in ('text.d2_tiles', 'text.d3_tiles'):
            seed_layout(m, inbox, tiles_of({'display': 'map'}))
            ha.log.clear()
            await m.answer_camera(request([[0, 0, 240, 200, 0, 0]], inbox=inbox))
            self.assertEqual(sent(ha), [], inbox)

    async def test_moving_someone_or_editing_a_zone_marks_the_card_dirty(self):
        ha = ready(fake_ha())
        m = self.manager(ha, tiles_of({'display': 'map', 'map': ['device_tracker.phone']}))
        related = set(m.related_entities({'entity': 'person.robin',
                                          'options': {'display': 'map', 'map': ['device_tracker.phone']}}))
        self.assertIn('device_tracker.phone', related)
        self.assertIn('zone.home', related)
        # An ordinary person tile reads nothing besides itself, as before.
        self.assertEqual(m.related_entities({'entity': 'person.robin', 'options': {}}), ())


@unittest.skipUnless(HAS_PIL, 'needs Pillow')
class Composing(unittest.TestCase):
    """What the composer and the request parser do with a map, without a Manager."""

    def test_the_request_parser_takes_a_person_beside_a_camera(self):
        found = camera_feed.live_request({'tiles': 'camera.a,person.robin,media_player.m', 'size': '54',
                                          'bg': 'FFFFFF,FFFFFF,FFFFFF'}, atlas=True)
        self.assertEqual(found[0], ['camera.a', 'person.robin', 'media_player.m'])
        self.assertIsNone(camera_feed.live_request({'tiles': 'light.a', 'size': '54', 'bg': 'FFFFFF'}, atlas=True))

    def test_a_map_fills_its_card_and_keeps_its_name_band(self):
        options = {'person.robin': {'display': 'map'}, 'person.sam': {'display': 'map', 'overlay': 'none'}}
        modes = camera_feed.picture_modes({'firmware': MAP_FIRMWARE}, options.get, list(options))
        self.assertEqual(modes, [('fill', True), ('fill', False)])

    def test_the_map_gate_is_the_firmware_the_layout_asks_for(self):
        from core import MAP_MIN_FIRMWARE
        self.assertEqual(camera_feed.MAP_MIN_FIRMWARE, MAP_MIN_FIRMWARE)
        self.assertTrue(camera_feed.can_show_map({'board': 'guition', 'firmware_known': MAP_FIRMWARE}))
        self.assertFalse(camera_feed.can_show_map({'board': 'guition', 'firmware_known': '0.14.0'}))
        self.assertFalse(camera_feed.can_show_map({'board': 'cyd', 'firmware_known': MAP_FIRMWARE}))

    def test_a_provisional_frame_is_not_kept_so_a_reload_draws_it_again(self):
        """A map drawn on half a basemap must not be served again from the strip cache: a reload is its second try."""
        from PIL import Image
        feed = camera_feed.CameraFeed(lambda entity: None)
        atlas = camera_feed.tile_art.parse('[[0,0,120,90,0,0]]', (480, 480), 1)
        drawn = []

        def render(width, height):
            image = Image.new('RGB', (width, height), (10, 20, 30))
            image.info['map_provisional'] = len(drawn) == 0   # the first try had a hole, the second did not
            drawn.append((width, height))
            return image

        async def run():
            first = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertIsNotNone(first)
            self.assertEqual(feed.strips, {}, 'a provisional strip is not kept')
            again = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertIsNotNone(again)
            self.assertEqual(len(drawn), 2, 'the same mark was drawn again, because the first was provisional')
            self.assertTrue(feed.strips, 'a whole strip is kept as before')
        asyncio.run(run())

    def test_a_strip_that_came_from_the_cache_still_says_the_map_has_a_picture(self):
        """A cache hit skips the drawing, so a drawn entity must not then be reported as having no picture."""
        from PIL import Image
        feed = camera_feed.CameraFeed(lambda entity: None)
        atlas = camera_feed.tile_art.parse('[[0,0,120,90,0,0]]', (480, 480), 1)

        def render(width, height):
            return Image.new('RGB', (width, height), (10, 20, 30))

        async def run():
            first = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertEqual(first[2], ['person.robin'])
            again = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertEqual(again[0], first[0], 'the same mark is the same strip')
            self.assertEqual(again[2], ['person.robin'], 'a cached strip still carries its map')
        asyncio.run(run())

    def test_a_rendered_frame_joins_the_strip_under_its_own_fingerprint(self):
        from PIL import Image
        feed = camera_feed.CameraFeed(lambda entity: None)
        atlas = camera_feed.tile_art.parse('[[0,0,120,90,0,0]]', (480, 480), 1)
        drawn = []

        def render(width, height, mark=(0,)):
            drawn.append((width, height))
            return Image.new('RGB', (width, height), (mark[0], 40, 60))

        async def run():
            first = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertIsNotNone(first)
            self.assertEqual(first[2], ['person.robin'])
            again = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-1', render)})
            self.assertEqual(again[0], first[0], 'the same mark is the same strip')
            moved = await feed.live(['person.robin'], 54, [0xFFFFFF], [0], atlas=atlas,
                                    renders={'person.robin': ('mark-2', render)})
            self.assertNotEqual(moved[0], first[0], 'a changed mark is a changed strip')
        asyncio.run(run())
        self.assertEqual(drawn[0], (120, 90), 'the renderer is asked for exactly the frame')

    def test_a_stale_170_is_normalized_only_on_the_maps_own_frame(self):
        """Firmware/WASM built before 0.14.0 sends 170 (card darkening) for every card_art tile, maps included;
        current firmware sends 0 for a map, but a screen still on the old build sends 170 regardless. Only the
        render's own frame is the stale mistake here -- its coordinates, size, radius, the canvas, and any other
        frame's own requested shade (a camera or a cover that really wants 170) must reach tile_art.encode as sent."""
        from PIL import Image

        async def fetch(entity):
            raise ConnectionError('no camera in this test')

        feed = camera_feed.CameraFeed(fetch)
        atlas = camera_feed.tile_art.parse('[[0,0,120,90,16,170],[120,0,100,80,8,170]]', (220, 90), 2)

        def render(width, height):
            return Image.new('RGB', (width, height), (10, 20, 30))

        captured = {}

        def fake_encode(raws, grounds, atlas, modes=None, compact=False):
            captured['atlas'] = atlas
            return b''

        with mock.patch.object(camera_feed.tile_art, 'encode', fake_encode):
            asyncio.run(feed.live(['person.robin', 'camera.hall'], 54, [0xFFFFFF, 0xFFFFFF], [0, 15], atlas=atlas,
                                  renders={'person.robin': ('mark-1', render)}))
        width, height, frames = captured['atlas']
        self.assertEqual((width, height), (220, 90), 'the canvas is untouched')
        self.assertEqual(frames[0], (0, 0, 120, 90, 16, 0), "the map's own frame is normalized to 0")
        self.assertEqual(frames[1], (120, 0, 100, 80, 8, 170), "another frame's requested shade is untouched")


@unittest.skipUnless(HAS_AIOHTTP and HAS_PIL, 'needs aiohttp and Pillow')
class Saving(unittest.IsolatedAsyncioTestCase):
    """A map is refused before it is saved on a screen that cannot draw one (app 0.4.24)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def manager(self, ha):
        m = Manager(with_screen_grid(ha), Path(self.tmp.name) / 'screens.json')
        m.inventory = lambda: (m.screens(), [{'id': 'person.robin'}, {'id': 'person.sam'}])
        m.write_layouts = lambda layouts: None
        m.notify = lambda: None
        return m

    def layout(self):
        return {'title': 'Hall', 'tiles': [{'entity': 'person.robin', 'name': '', 'options': {'display': 'map'}}]}

    async def test_a_board_that_draws_no_pictures_hears_about_the_map_and_not_about_a_camera(self):
        ha = ready(fake_ha())
        m = self.manager(ha)
        with self.assertRaisesRegex(ValueError, 'cannot show a map'):
            m.save('text.d2_tiles', self.layout())
        # An update makes no room for pictures on that board, so it hears the same thing however new its firmware.
        ha.states['sensor.d2_fw']['state'] = MAP_FIRMWARE
        m._screens_key = None
        with self.assertRaisesRegex(ValueError, 'cannot show a map'):
            m.save('text.d2_tiles', self.layout())

    async def test_a_screen_below_the_gate_is_told_which_firmware_it_needs(self):
        ha = ready(fake_ha())
        ha.states['sensor.d3_fw']['state'] = '0.14.0'
        m = self.manager(ha)
        with self.assertRaisesRegex(ValueError, '0.15.0'):
            m.save('text.d3_tiles', self.layout())

    async def test_a_screen_at_the_gate_saves_its_map(self):
        ha = ready(fake_ha())
        m = self.manager(ha)
        m.save('text.d1_tiles', self.layout())
        self.assertEqual(m.layouts['text.d1_tiles']['tiles'][0]['options'], {'display': 'map'})

    async def test_a_device_tracker_is_still_a_top_bar_entity_and_no_tile(self):
        from core import DOMAINS, HEADER_ONLY_DOMAINS, header_entity
        self.assertIn('device_tracker', HEADER_ONLY_DOMAINS)
        self.assertNotIn('device_tracker', DOMAINS)
        self.assertTrue(header_entity('device_tracker.phone'))
