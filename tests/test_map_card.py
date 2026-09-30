"""The map card (app 0.4.24, firmware 0.15.0): the add-on works out the geometry, the movement mark and the picture.

No test here reaches the network. The basemap is handed in as a plain image, and the card's own words come from the
translations, so what these tests check is the arithmetic and the pixels, never Home Assistant.
"""
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'screen_manager/app'))
import map_card  # noqa: E402

HAS_PIL = importlib.util.find_spec('PIL') is not None

HOME = {'state': 'zoning', 'attributes': {'friendly_name': 'Home', 'latitude': 52.0, 'longitude': 5.0, 'radius': 100}}
WORK = {'state': 'zoning', 'attributes': {'friendly_name': 'Work', 'latitude': 52.02, 'longitude': 5.03, 'radius': 150}}


def person(lat=None, lon=None, state='home', name='Robin'):
    attributes = {'friendly_name': name}
    if lat is not None:
        attributes.update({'latitude': lat, 'longitude': lon})
    return {'state': state, 'attributes': attributes}


def states(**extra):
    return {'zone.home': dict(HOME), **extra}


def tile(entity='person.robin', **options):
    return {'entity': entity, 'name': 'Robin', 'options': {'display': 'map', **options}}


class Projection(unittest.TestCase):
    """Web Mercator, the projection Home Assistant's own basemap is drawn in."""

    def test_the_middle_of_the_frame_is_the_middle_of_the_viewport(self):
        view = map_card.viewport([(52.0, 5.0)], [], 'fit', (320, 240), map_card.MAX_TILES)
        x, y = map_card.project(view.lat, view.lon, view, (320, 240))
        self.assertAlmostEqual(x, 160, places=3)
        self.assertAlmostEqual(y, 120, places=3)

    def test_a_point_round_trips_at_the_equator_and_at_sixty_north(self):
        for lat, lon in ((0.0, 0.0), (60.0, 24.9), (-33.9, 151.2)):
            view = map_card.viewport([(lat, lon)], [], '15', (400, 300), map_card.MAX_TILES)
            for dlat, dlon in ((0.0, 0.0), (0.001, 0.002), (-0.002, 0.001)):
                x, y = map_card.project(lat + dlat, lon + dlon, view, (400, 300))
                back = map_card.unproject(x, y, view, (400, 300))
                self.assertAlmostEqual(back[0], lat + dlat, places=6)
                self.assertAlmostEqual(back[1], lon + dlon, places=6)

    def test_y_grows_southward_and_x_eastward(self):
        view = map_card.viewport([(52.0, 5.0)], [], '15', (320, 240), map_card.MAX_TILES)
        north = map_card.project(52.001, 5.0, view, (320, 240))
        south = map_card.project(51.999, 5.0, view, (320, 240))
        east = map_card.project(52.0, 5.001, view, (320, 240))
        self.assertLess(north[1], south[1])
        self.assertGreater(east[0], 160)

    def test_a_degree_of_latitude_covers_more_pixels_up_north_than_at_the_equator(self):
        # Mercator stretches towards the poles: at 60 degrees a degree of latitude is about twice the pixels.
        polar = map_card.viewport([(60.0, 0.0)], [], '11', (320, 240), map_card.MAX_TILES)
        equator = map_card.viewport([(0.0, 0.0)], [], '11', (320, 240), map_card.MAX_TILES)
        tall = map_card.project(60.1, 0.0, polar, (320, 240))[1] - map_card.project(60.0, 0.0, polar, (320, 240))[1]
        flat = map_card.project(0.1, 0.0, equator, (320, 240))[1] - map_card.project(0.0, 0.0, equator, (320, 240))[1]
        self.assertLess(tall, 0)  # north is up, so a step north is a smaller y
        self.assertLess(flat, 0)
        self.assertGreater(abs(tall), abs(flat) * 1.5)


class Viewport(unittest.TestCase):
    def test_two_points_fit_with_padding_on_every_side(self):
        points = [(52.00, 5.00), (52.02, 5.04)]
        view = map_card.viewport(points, [], 'fit', (320, 240), map_card.MAX_TILES)
        xs, ys = [], []
        for lat, lon in points:
            x, y = map_card.project(lat, lon, view, (320, 240))
            xs.append(x)
            ys.append(y)
        # Inside the frame, and at least a tenth of the frame away from its edges: the 12 % padding.
        self.assertTrue(all(0 < value < 320 for value in xs), xs)
        self.assertTrue(all(0 < value < 240 for value in ys), ys)
        self.assertGreater(min(xs), 320 * 0.05)
        self.assertLess(max(xs), 320 * 0.95)
        self.assertGreater(min(ys), 240 * 0.05)
        self.assertLess(max(ys), 240 * 0.95)

    def test_one_point_clamps_to_the_closest_zoom(self):
        view = map_card.viewport([(52.0, 5.0)], [], 'fit', (320, 240), map_card.MAX_TILES)
        self.assertEqual(view.zoom, map_card.FIT_MAX_ZOOM)
        self.assertEqual(view.zoom, 17)

    def test_two_points_on_opposite_sides_of_the_world_clamp_to_the_widest_zoom(self):
        view = map_card.viewport([(10.0, -170.0), (-10.0, 170.0)], [], 'fit', (320, 240), map_card.MAX_TILES)
        self.assertEqual(view.zoom, map_card.FIT_MIN_ZOOM)
        self.assertEqual(view.zoom, 3)

    def test_a_fixed_zoom_centres_on_the_mean_of_the_points(self):
        view = map_card.viewport([(52.0, 5.0), (52.04, 5.10)], [], '13', (320, 240), map_card.MAX_TILES)
        self.assertEqual(view.zoom, 13)
        self.assertAlmostEqual(view.lat, 52.02, places=6)
        self.assertAlmostEqual(view.lon, 5.05, places=6)

    def test_a_zone_joins_the_fit_only_when_a_shown_entity_is_inside_it(self):
        near = map_card.zones(states(**{'zone.work': dict(WORK)}))
        inside = map_card.viewport([(52.0, 5.0)], near, 'fit', (320, 240), map_card.MAX_TILES)
        # Nobody is in Work, so the holiday zone cannot flatten the map: the fit is the home zone's own.
        self.assertEqual(inside.zoom, map_card.viewport([(52.0, 5.0)], [z for z in near if z.entity == 'zone.home'],
                                                       'fit', (320, 240), map_card.MAX_TILES).zoom)
        joined = map_card.viewport([(52.0, 5.0), (52.02, 5.03)], near, 'fit', (320, 240), map_card.MAX_TILES)
        self.assertLess(joined.zoom, inside.zoom)

    def test_no_coordinates_anywhere_centres_on_the_home_zone(self):
        view = map_card.viewport([], map_card.zones(states()), 'fit', (320, 240), map_card.MAX_TILES)
        self.assertAlmostEqual(view.lat, 52.0, places=6)
        self.assertAlmostEqual(view.lon, 5.0, places=6)
        self.assertEqual(view.zoom, map_card.HOME_ZOOM)

    def test_the_basemap_never_needs_more_tiles_than_the_budget(self):
        for size in ((64, 48), (160, 120), (320, 240), (480, 320), (800, 480), (1024, 640)):
            for zoom in map_card.MAP_ZOOMS:
                view = map_card.viewport([(52.0, 5.0), (52.3, 5.4)], [], zoom, size, map_card.MAX_TILES)
                self.assertLessEqual(view.tiles, map_card.MAX_TILES, (size, zoom))
                self.assertEqual(len(view.tile_keys()), view.tiles, (size, zoom))
                self.assertLessEqual(view.source_zoom, view.zoom, (size, zoom))

    def test_a_smaller_budget_takes_the_basemap_a_step_further_out(self):
        big = map_card.viewport([(52.0, 5.0)], [], '15', (1024, 640), map_card.MAX_TILES)
        small = map_card.viewport([(52.0, 5.0)], [], '15', (1024, 640), 4)
        self.assertLess(small.source_zoom, big.source_zoom)
        self.assertEqual(small.zoom, big.zoom)  # the map shows the same ground, from softer tiles
        self.assertLessEqual(small.tiles, 4)

    def test_every_tile_the_viewport_names_is_a_bounded_whole_number(self):
        view = map_card.viewport([(52.0, 5.0)], [], '15', (480, 320), map_card.MAX_TILES)
        for z, x, y in view.tile_keys():
            self.assertEqual((z, x, y), (int(z), int(x), int(y)))
            self.assertTrue(0 <= z <= 19, z)
            self.assertTrue(0 <= x < 2 ** z and 0 <= y < 2 ** z, (x, y))


class Positions(unittest.TestCase):
    def test_coordinates_make_a_point(self):
        found = map_card.positions(['person.robin'], states(**{'person.robin': person(52.01, 5.02)}))
        self.assertEqual([item.kind for item in found], ['point'])
        self.assertAlmostEqual(found[0].lat, 52.01)
        self.assertAlmostEqual(found[0].lon, 5.02)
        self.assertEqual(found[0].name, 'Robin')

    def test_a_zone_name_without_coordinates_is_approximate(self):
        found = map_card.positions(['person.sam'], states(**{'zone.work': dict(WORK),
                                                             'person.sam': person(state='Work', name='Sam')}))
        self.assertEqual([item.kind for item in found], ['zone'])
        self.assertAlmostEqual(found[0].lat, 52.02)
        self.assertAlmostEqual(found[0].lon, 5.03)

    def test_home_without_coordinates_sits_in_the_home_zone(self):
        found = map_card.positions(['person.sam'], states(**{'person.sam': person(state='home', name='Sam')}))
        self.assertEqual([item.kind for item in found], ['zone'])
        self.assertAlmostEqual(found[0].lat, 52.0)

    def test_not_home_without_coordinates_is_off_the_map(self):
        found = map_card.positions(['person.sam'], states(**{'person.sam': person(state='not_home', name='Sam')}))
        self.assertEqual([item.kind for item in found], ['away'])
        self.assertIsNone(found[0].lat)

    def test_an_unavailable_entity_is_off_the_map_and_grey(self):
        for state in ('unavailable', 'unknown'):
            found = map_card.positions(['device_tracker.phone'],
                                       states(**{'device_tracker.phone': person(state=state, name='Phone')}))
            self.assertEqual([item.kind for item in found], ['unknown'], state)
            self.assertEqual(map_card.marker_color(found[0]), 'unknown', state)

    def test_a_marker_is_coloured_by_where_someone_is(self):
        home = map_card.positions(['person.robin'], states(**{'person.robin': person(52.0, 5.0, state='home')}))[0]
        away = map_card.positions(['person.robin'], states(**{'person.robin': person(51.0, 4.0, state='not_home')}))[0]
        zoned = map_card.positions(['person.robin'], states(**{'zone.work': dict(WORK),
                                                              'person.robin': person(52.02, 5.03, state='Work')}))[0]
        self.assertEqual(map_card.marker_color(home), 'home')
        self.assertEqual(map_card.marker_color(away), 'away')
        self.assertEqual(map_card.marker_color(zoned), 'zone')

    def test_a_coordinate_outside_the_world_is_no_position(self):
        for lat, lon in ((91.0, 5.0), (52.0, 181.0), (float('nan'), 5.0)):
            found = map_card.positions(['device_tracker.car'],
                                       states(**{'device_tracker.car': person(lat, lon, state='not_home')}))
            self.assertEqual([item.kind for item in found], ['away'], (lat, lon))

    def test_the_shown_entities_are_the_tile_and_its_companions_in_order(self):
        self.assertEqual(map_card.shown_entities(tile(map=['device_tracker.phone', 'person.sam'])),
                         ['person.robin', 'device_tracker.phone', 'person.sam'])
        self.assertEqual(map_card.shown_entities(tile()), ['person.robin'])

    def test_a_companion_list_is_bounded_even_when_the_store_holds_more(self):
        many = [f'person.p{n}' for n in range(20)]
        self.assertEqual(len(map_card.shown_entities(tile(map=many))), map_card.MAP_MAX_ENTITIES)

    def test_only_a_person_or_a_device_tracker_is_supported(self):
        self.assertTrue(map_card.supported('person.robin'))
        self.assertTrue(map_card.supported('device_tracker.phone'))
        for value in ('light.kitchen', 'zone.home', '', None, 'person', 'PERSON.robin', 'person.' + 'x' * 130):
            self.assertFalse(map_card.supported(value), value)


class Zones(unittest.TestCase):
    def test_every_zone_with_a_place_is_read_with_its_name_and_radius(self):
        found = map_card.zones(states(**{'zone.work': dict(WORK), 'light.kitchen': {'state': 'on', 'attributes': {}}}))
        self.assertEqual(sorted(zone.entity for zone in found), ['zone.home', 'zone.work'])
        work = next(zone for zone in found if zone.entity == 'zone.work')
        self.assertEqual((work.name, work.radius), ('Work', 150.0))

    def test_a_zone_without_coordinates_is_left_out(self):
        broken = states(**{'zone.nowhere': {'state': 'zoning', 'attributes': {'friendly_name': 'Nowhere'}}})
        self.assertEqual([zone.entity for zone in map_card.zones(broken)], ['zone.home'])

    def test_the_home_zone_is_found_by_its_entity_id(self):
        self.assertIsNotNone(map_card.home_zone(map_card.zones(states())))
        self.assertIsNone(map_card.home_zone([]))


class Mark(unittest.TestCase):
    """The movement mark: it changes when the picture would change, and never because of the frame."""

    def test_the_mark_does_not_depend_on_the_frame_the_look_or_the_basemap_image(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        one = map_card.fingerprint(tile(), data)
        self.assertEqual(one, map_card.fingerprint(tile(), data))
        # Nothing about the frame is an argument of the mark at all: size, look and tiles are part of the wish.
        self.assertEqual(sorted(map_card.fingerprint.__code__.co_varnames[:map_card.fingerprint.__code__.co_argcount]),
                         ['states', 'tile'])

    def test_the_mark_is_short_enough_for_the_state_message(self):
        mark = map_card.extras(tile(), states(**{'person.robin': person(52.0, 5.0)}))['mk']
        self.assertTrue(re.fullmatch(r'[0-9a-f]{8,16}', mark), mark)

    def test_the_mark_holds_while_someone_moves_inside_one_cell_and_changes_when_they_leave_it(self):
        base = map_card.fingerprint(tile(), states(**{'person.robin': person(52.0, 5.0)}))
        # About 5 m north: inside the 25 m cell.
        still = map_card.fingerprint(tile(), states(**{'person.robin': person(52.000045, 5.0)}))
        self.assertEqual(base, still)
        # About 200 m north: several cells away.
        moved = map_card.fingerprint(tile(), states(**{'person.robin': person(52.0018, 5.0)}))
        self.assertNotEqual(base, moved)

    def test_a_step_east_changes_the_mark_as_much_as_a_step_north(self):
        base = map_card.fingerprint(tile(), states(**{'person.robin': person(52.0, 5.0)}))
        east = map_card.fingerprint(tile(), states(**{'person.robin': person(52.0, 5.003)}))
        self.assertNotEqual(base, east)

    def test_the_mark_follows_every_stored_choice_of_the_card(self):
        data = states(**{'person.robin': person(52.0, 5.0), 'person.sam': person(52.01, 5.01, name='Sam'),
                         'device_tracker.phone': person(52.02, 5.02, name='Phone')})
        base = map_card.fingerprint(tile(), data)
        for options in ({'map': ['person.sam']}, {'zoom': '13'}, {'labels': 'initials'}, {'basemap': 'none'},
                        {'overlay': 'none'}):
            self.assertNotEqual(base, map_card.fingerprint(tile(**options), data), options)
        # The order companions are drawn in is a choice of its own.
        first = map_card.fingerprint(tile(map=['person.sam', 'device_tracker.phone']), data)
        second = map_card.fingerprint(tile(map=['device_tracker.phone', 'person.sam']), data)
        self.assertNotEqual(first, second)

    def test_the_mark_follows_a_moved_zone_and_an_entity_going_away(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        base = map_card.fingerprint(tile(), data)
        moved = map_card.fingerprint(tile(), states(**{'person.robin': person(52.0, 5.0), 'zone.home':
                                                      {**HOME, 'attributes': {**HOME['attributes'], 'latitude': 52.5}}}))
        self.assertNotEqual(base, moved)
        wider = dict(HOME)
        wider['attributes'] = {**HOME['attributes'], 'radius': 400}
        self.assertNotEqual(base, map_card.fingerprint(tile(), {'zone.home': wider, 'person.robin': person(52.0, 5.0)}))
        added = map_card.fingerprint(tile(), {**data, 'zone.work': dict(WORK)})
        self.assertNotEqual(base, added)
        gone = map_card.fingerprint(tile(), states(**{'person.robin': person(state='unavailable')}))
        self.assertNotEqual(base, gone)

    def test_a_zone_nobody_can_see_does_not_change_the_mark(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        base = map_card.fingerprint(tile(), data)
        noise = {**data, 'light.kitchen': {'state': 'on', 'attributes': {'brightness': 4}}}
        self.assertEqual(base, map_card.fingerprint(tile(), noise))

    def test_a_tile_that_is_no_map_has_no_mark(self):
        self.assertIsNone(map_card.extras({'entity': 'person.robin', 'name': 'Robin', 'options': {}},
                                          states(**{'person.robin': person(52.0, 5.0)})))


def zoned(data, count=12):
    """`count` zones around the home zone, as a household with a lot of places has."""
    for n in range(count):
        data[f'zone.z{n:02d}'] = {'state': 'zoning', 'attributes': {
            'friendly_name': f'Zone {n}', 'latitude': 52.0 + n * 0.003, 'longitude': 5.0 + n * 0.004, 'radius': 120}}
    return data


def crowd(count=map_card.MAP_MAX_ENTITIES):
    """(the tile, the states) of a card with `count` entities on it, the first its own."""
    data = states()
    companions = []
    for n in range(count):
        entity = 'person.robin' if n == 0 else (f'person.p{n}' if n % 2 else f'device_tracker.d{n}')
        data[entity] = person(52.0 + n * 0.004, 5.0 + n * 0.006, name=f'Person Name {n}')
        if n:
            companions.append(entity)
    return tile(map=companions), data


def plain(colour=(200, 70, 50), size=(480, 320)):
    """A basemap as map_tiles hands one over: RGBA, already cropped to the frame."""
    from PIL import Image
    return Image.new('RGBA', size, colour + (255,))


SIZES = ((64, 48), (160, 80), (232, 232), (320, 240), (480, 320), (800, 480), (1024, 640))


@unittest.skipUnless(HAS_PIL, 'Pillow comes with ESPHome in the add-on image')
class Render(unittest.TestCase):
    """The picture itself. The add-on draws the whole frame, so the screen only places the bitmap."""

    def test_a_render_is_exactly_the_frame_it_was_asked_for(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        for size in SIZES:
            image = map_card.render(size, tile(), data)
            self.assertEqual(image.size, size, size)
            self.assertEqual(image.mode, 'RGB', size)

    def test_a_card_with_nobody_anywhere_still_draws_and_says_so(self):
        data = states(**{'person.robin': person(state='unavailable')})
        for size in SIZES:
            self.assertEqual(map_card.render(size, tile(), data).size, size, size)
        self.assertEqual(map_card.frame((320, 240), tile(), data).note, 'no_location')

    def test_a_card_without_a_basemap_is_drawn_from_the_zones_alone(self):
        data = zoned(states(**{'person.robin': person(52.0, 5.0)}))
        frame = map_card.frame((480, 320), tile(), data, basemap=False)
        self.assertFalse(frame.basemap)
        self.assertEqual(map_card.render((480, 320), tile(), data, basemap_image=None).size, (480, 320))
        self.assertGreater(len(frame.rings), 0)

    def test_every_marker_and_every_label_stays_inside_the_card(self):
        card, data = crowd()
        for size in SIZES:
            frame = map_card.frame(size, card, data)
            self.assertTrue(frame.markers, size)
            for marker in frame.markers:
                self.assertTrue(0 <= marker.x <= size[0], (size, marker.entity, marker.x))
                self.assertTrue(0 <= marker.y <= size[1] - frame.band, (size, marker.entity, marker.y))
                if marker.label_box:
                    left, top, right, bottom = marker.label_box
                    self.assertTrue(0 <= left < right <= size[0], (size, marker.label_box))
                    self.assertTrue(0 <= top < bottom <= size[1] - frame.band, (size, marker.label_box))

    def test_the_fit_is_made_for_the_map_area_and_not_for_the_whole_frame(self):
        """The bottom of the frame is the name band and the legend, so the fit has to happen above them.

        Fitted to the whole frame, the people at the bottom of the bounding box would land under the legend and be
        clamped onto its edge; and the basemap would then no longer be under the markers."""
        card, data = crowd(4)
        size = (480, 320)
        out = map_card.frame(size, card, data)
        floor = out.map_area[3]
        self.assertLess(floor, size[1] - out.band, 'the legend takes room of its own')
        for marker in out.markers:
            # Inside the map area on its own, not sitting on the edge the clamp would have put it on.
            self.assertGreater(marker.y, marker.radius + 1, marker.entity)
            self.assertLess(marker.y, floor - marker.radius - 1, marker.entity)
        # The basemap still covers the whole frame, and the middle of the map area is the middle of the fit.
        self.assertEqual((out.view.width, out.view.height), size)
        placed = [item for item in map_card.positions(map_card.shown_entities(card), data) if item.placed]
        xs = [map_card.project(item.lat, item.lon, out.view, size) for item in placed]
        self.assertAlmostEqual(sum(x for x, _ in xs) / len(xs), size[0] / 2, delta=size[0] * 0.08)
        self.assertAlmostEqual(sum(y for _, y in xs) / len(xs), floor / 2, delta=floor * 0.08)

    def test_the_basemap_is_fetched_for_exactly_the_viewport_the_markers_are_drawn_in(self):
        card, data = crowd(3)
        for size in ((320, 240), (480, 320), (1024, 640)):
            out = map_card.frame(size, card, data, basemap=True)
            view = map_card.view_of(size, card, data)
            for name in ('lat', 'lon', 'zoom', 'source_zoom', 'width', 'height', 'offset'):
                self.assertEqual(getattr(view, name), getattr(out.view, name), (size, name))

    def test_the_legend_folds_instead_of_overflowing(self):
        card, data = crowd()
        small = map_card.frame((160, 80), card, data)
        self.assertEqual(small.legend, [])
        square = map_card.frame((232, 232), card, data)
        self.assertLessEqual(len(square.legend), map_card.LEGEND_ROWS)
        self.assertEqual(len(square.legend) + square.more, map_card.MAP_MAX_ENTITIES)
        self.assertGreater(square.more, 0)
        page = map_card.frame((800, 480), card, data)
        self.assertEqual(len(page.legend), map_card.MAP_MAX_ENTITIES)
        self.assertEqual(page.more, 0)
        self.assertEqual(page.columns, 2)
        self.assertEqual(map_card.frame((480, 800), card, data).columns, 1)

    def test_the_attribution_is_drawn_whenever_basemap_tiles_are(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        self.assertIn('OpenStreetMap', map_card.ATTRIBUTION)
        for size in ((320, 240), (480, 320), (1024, 640)):
            frame = map_card.frame(size, tile(), data, basemap=True)
            pill = frame.attribution
            self.assertIsNotNone(pill, size)
            left, top, right, bottom = pill
            self.assertGreaterEqual(frame.attribution_text, map_card.ATTRIBUTION_MIN_TEXT, size)
            self.assertTrue(0 <= left < right <= size[0], (size, pill))
            self.assertTrue(0 <= top < bottom <= size[1] - frame.band, (size, pill))
            # Bottom right of the map area, above the band the screen writes the name in.
            self.assertGreater(left, size[0] / 2, (size, pill))

    def test_a_narrow_card_shrinks_the_pill_to_the_floor_before_giving_up_the_basemap(self):
        """A 1x1 card is narrow, not short: the attribution gets smaller, down to the legible floor, and the streets

        stay. Only a frame that cannot carry even a 9 px pill is drawn schematic (plan 3.5)."""
        data = states(**{'person.robin': person(52.0, 5.0)})
        for size in ((150, 118), (160, 120), (232, 232)):
            out = map_card.frame(size, tile(), data, basemap=True)
            self.assertTrue(out.basemap, size)
            self.assertIsNotNone(out.attribution, size)
            left, top, right, bottom = out.attribution
            self.assertGreaterEqual(out.attribution_text, map_card.ATTRIBUTION_MIN_TEXT, size)
            self.assertTrue(0 <= left < right <= size[0], (size, out.attribution))
            self.assertTrue(0 <= top < bottom <= out.map_area[3], (size, out.attribution))
        # A wide card keeps the pill at its full size, bottom right.
        wide = map_card.frame((480, 320), tile(), data, basemap=True)
        narrow = map_card.frame((150, 118), tile(), data, basemap=True)
        self.assertGreater(wide.attribution_text, narrow.attribution_text)
        # A card too narrow for even the smallest legible pill goes without streets rather than without the licence.
        self.assertFalse(map_card.frame((70, 118), tile(), data, basemap=True).basemap)

    def test_a_frame_too_short_for_a_legible_pill_uses_no_basemap_at_all(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        short = (320, map_card.ATTRIBUTION_MIN_HEIGHT - 8)
        self.assertFalse(map_card.wants_basemap(tile(), short))
        self.assertIsNone(map_card.frame(short, tile(), data, basemap=True).attribution)
        offered = map_card.render(short, tile(), data, basemap_image=plain(size=short))
        self.assertEqual(offered.tobytes(), map_card.render(short, tile(), data).tobytes())

    def test_with_no_basemap_chosen_nothing_is_fetched_and_nothing_is_attributed(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        self.assertTrue(map_card.wants_basemap(tile(), (480, 320)))
        self.assertFalse(map_card.wants_basemap(tile(basemap='none'), (480, 320)))
        self.assertIsNone(map_card.frame((480, 320), tile(basemap='none'), data, basemap=False).attribution)

    def test_the_dark_look_desaturates_and_darkens_the_basemap_rather_than_inverting_it(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        light = map_card.render((480, 320), tile(), data, basemap_image=plain())
        night = map_card.render((480, 320), tile(), data, dark=True, basemap_image=plain())
        self.assertNotEqual(light.tobytes(), night.tobytes())
        one, two = light.getpixel((2, 2)), night.getpixel((2, 2))
        self.assertLess(sum(two), sum(one))                            # darker
        self.assertLess(max(two) - min(two), max(one) - min(one))      # and less colour
        self.assertEqual(two.index(max(two)), 0)                       # still reddish: not inverted

    def test_two_renders_of_the_same_card_are_the_same_bytes(self):
        card, data = crowd()
        data = zoned(data)
        first = map_card.render((480, 320), card, data, basemap_image=plain())
        second = map_card.render((480, 320), card, data, basemap_image=plain())
        self.assertEqual(first.tobytes(), second.tobytes())

    def test_the_render_cache_answers_the_second_time(self):
        data = states(**{'person.robin': person(52.0, 5.0)})
        cache = {}
        one = map_card.render((320, 240), tile(), data, cache=cache)
        self.assertIs(one, map_card.render((320, 240), tile(), data, cache=cache))
        self.assertEqual(len(cache), 1)
        self.assertIsNot(one, map_card.render((480, 320), tile(), data, cache=cache))
        self.assertEqual(len(cache), 2)
        map_card.render((320, 240), tile(), data, dark=True, cache=cache)
        self.assertEqual(len(cache), 3)
        moved = states(**{'person.robin': person(52.02, 5.02)})
        map_card.render((320, 240), tile(), moved, cache=cache)
        self.assertEqual(len(cache), 4)

    def test_a_card_drawn_on_half_a_basemap_is_never_kept(self):
        """A basemap with a hole in it is provisional: drawing it is right, keeping it is not.

        The movement mark has not changed, so nothing would ever ask for the card again; the render cache therefore
        keeps it out, and the picture says so, so the strip is not kept either (camera_feed.live)."""
        data = states(**{'person.robin': person(52.0, 5.0)})
        half = plain()
        half.putalpha(0)                       # every tile missing, as a mosaic that came back empty would be
        half.info['map_missing'] = 2
        cache = {}
        drawn = map_card.render((480, 320), tile(), data, basemap_image=half, cache=cache)
        self.assertEqual(cache, {})
        self.assertTrue(drawn.info.get('map_provisional'))
        # A whole basemap is kept, and says nothing is missing.
        whole = plain()
        whole.info['map_missing'] = 0
        kept = map_card.render((480, 320), tile(), data, basemap_image=whole, cache=cache)
        self.assertEqual(len(cache), 1)
        self.assertFalse(kept.info.get('map_provisional'))
        # A card with no basemap at all is whole: `basemap: none` is not a hole.
        schematic = map_card.render((480, 320), tile(basemap='none'), data, cache=cache)
        self.assertFalse(schematic.info.get('map_provisional'))
        self.assertEqual(len(cache), 2)

    def test_a_busy_card_renders_in_well_under_a_tenth_of_a_second(self):
        card, data = crowd()
        data = zoned(data)
        basemap = plain()
        map_card.render((480, 320), card, data, basemap_image=basemap)  # the font and Pillow are loaded once

        def cold():
            map_card._MEASURED.clear()   # a card nothing has been measured for yet, as the first one of a page is
            return _timed(lambda: map_card.render((480, 320), card, data, basemap_image=basemap))
        # The floor of several tries: the fastest run is the real cost, where an average would measure whatever
        # else the machine was doing at the time.
        best = min(cold() for _ in range(5))
        self.assertLess(best, 0.05, f'{best * 1000:.0f} ms for eight entities and twelve zones at 480x320')


class FontDiscovery(unittest.TestCase):
    """Where `_font_files` looks, in a checkout and in the built add-on, where the module sits right under `/`."""

    def _candidates(self, module_path, bold=False):
        original = map_card.__file__
        map_card.__file__ = module_path
        try:
            return map_card._font_files(bold)
        finally:
            map_card.__file__ = original

    def test_a_shallow_app_directory_does_not_crash_font_discovery(self):
        # The add-on's Dockerfile copies `app` to `/app`, so `map_card.py` sits right under the filesystem root and
        # has only one parent -- indexing a second one, as the add-on's crash traceback did, raises IndexError.
        candidates = self._candidates('/app/map_card.py')
        self.assertTrue(candidates)

    def test_a_repository_checkout_still_finds_its_own_fonts_first(self):
        candidates = self._candidates(str(ROOT / 'screen_manager/app/map_card.py'))
        self.assertEqual(candidates[0], ROOT / 'fonts' / 'Roboto-400.ttf')

    def test_the_last_candidate_is_always_a_system_font(self):
        for module_path in ('/app/map_card.py', str(ROOT / 'screen_manager/app/map_card.py')):
            candidates = self._candidates(module_path)
            self.assertEqual(candidates[-1], Path('/usr/share/fonts/truetype/dejavu') / 'DejaVuSans.ttf')


def _timed(work):
    start = time.perf_counter()
    work()
    return time.perf_counter() - start


sys.path.insert(0, str(ROOT / 'tests'))
import core  # noqa: E402


def layout(**options):
    return {'title': 'Hall', 'tiles': [{'entity': 'person.robin', 'name': '', 'options': {'display': 'map', **options}}]}


def kept(**options):
    return core.validate_layout(layout(**options))['tiles'][0].get('options', {})


class Options(unittest.TestCase):
    """The map's own options, validated the same way in the add-on and in the editor."""

    def test_the_stored_choices_of_a_map_tile_are_kept(self):
        self.assertEqual(kept(map=['device_tracker.a'], zoom='15', basemap='none'),
                         {'display': 'map', 'map': ['device_tracker.a'], 'zoom': '15', 'basemap': 'none'})
        self.assertEqual(kept(labels='initials', overlay='none'),
                         {'display': 'map', 'labels': 'initials', 'overlay': 'none'})

    def test_the_first_choice_of_each_is_never_stored(self):
        self.assertEqual(kept(zoom='fit', labels='names', basemap='auto', overlay='name'), {'display': 'map'})

    def test_another_display_leaves_no_map_settings_behind(self):
        self.assertEqual(core.validate_layout({'title': 'Hall', 'tiles': [
            {'entity': 'person.robin', 'name': '', 'options': {
                'display': 'standard', 'map': ['person.sam'], 'zoom': '13', 'labels': 'initials', 'basemap': 'none'}}]}
        )['tiles'][0]['options'], {'display': 'standard'})

    def test_a_map_is_refused_what_it_cannot_draw(self):
        for options in ({'map': [f'person.p{n}' for n in range(8)]},          # eight companions is one too many
                        {'map': ['person.sam', 'person.sam']},                # the same entity twice
                        {'map': ['person.robin']},                            # the tile's own entity
                        {'map': ['light.kitchen']},                           # not a person or a device tracker
                        {'map': 'person.sam'},                                # not a list
                        {'basemap': 'satellite'},
                        {'zoom': '19'},
                        {'labels': 'emoji'},
                        {'fit': 'contain'}):                                  # a map is always its frame's own size
            with self.subTest(options=options), self.assertRaises(ValueError):
                core.validate_layout(layout(**options))

    def test_seven_companions_make_the_eight_a_card_may_show(self):
        self.assertEqual(len(kept(map=[f'person.p{n}' for n in range(7)])['map']), 7)

    def test_a_map_names_the_firmware_it_needs(self):
        self.assertEqual(core.min_firmware(core.validate_layout(layout())), core.MAP_MIN_FIRMWARE)
        self.assertEqual(core.MAP_MIN_FIRMWARE[2], 0, 'a feature gate names a shared X.Y.0')
        self.assertEqual(core.MAP_DOMAINS, map_card.MAP_DOMAINS)
        self.assertEqual(core.MAP_MAX_ENTITIES, map_card.MAP_MAX_ENTITIES)
        self.assertEqual(core.MAP_ZOOMS, map_card.MAP_ZOOMS)
        self.assertEqual(core.MAP_LABELS, map_card.MAP_LABELS)
        self.assertEqual(core.MAP_BASEMAPS, map_card.MAP_BASEMAPS)

    def test_map_is_a_display_a_person_offers(self):
        self.assertIn('map', core.DISPLAYS['person'])
        self.assertEqual(core.DISPLAYS['person'][0], 'standard')

    def test_a_map_card_waits_behind_a_map_marker_and_not_behind_a_head(self):
        """The picture fills the card, so the icon only shows while it is on its way; a marker says what is coming."""
        import tile_icons
        marker = tile_icons.ICONS['map-marker'][0]
        made = core.validate_layout(layout())['tiles'][0]
        self.assertEqual(core.tile_icon(made, {}), marker)
        # A chosen icon and Home Assistant's own icon still win, and another display keeps the person's head.
        chosen = core.validate_layout(layout(**{}) | {})['tiles'][0]
        chosen['options'] = {**chosen['options'], 'icon': 'account'}
        self.assertEqual(core.tile_icon(chosen, {}), tile_icons.ICONS['account'][0])
        self.assertEqual(core.tile_icon(made, {'icon': 'mdi:home-map-marker'}), tile_icons.GLYPHS['home-map-marker'])
        plain = {'entity': 'person.robin', 'name': '', 'options': {}}
        self.assertNotEqual(core.tile_icon(plain, {}), marker)

    def test_no_coordinate_and_no_choice_of_the_map_reaches_a_screen(self):
        tile = core.validate_layout(layout(map=['device_tracker.phone'], zoom='13', labels='initials',
                                           basemap='none', overlay='none'))['tiles'][0]
        wire = core.screen_options(tile, {})
        # `display` and `overlay` the screen needs to place the picture and write the name on it; `icon` is the
        # marker it waits behind. Who is on the map, how far out it sits and where the streets come from stay here.
        import tile_icons
        self.assertEqual(wire, {'display': 'map', 'overlay': 'none', 'icon': tile_icons.ICONS['map-marker'][0]})
        for gone in ('map', 'zoom', 'labels', 'basemap'):
            self.assertNotIn(gone, wire)
        self.assertNotIn('device_tracker', json.dumps(wire))
