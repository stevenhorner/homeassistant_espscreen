"""The map card: where the people a screen follows are, drawn in the add-on (app 0.4.24, firmware 0.15.0).

A map tile is a pictured tile, like a live camera: the add-on draws the whole frame and the screen only places the
bitmap (components/smart_display/runtime_tiles.h, render_camera_card). So every bit of map arithmetic is here, and
no coordinate, token or tile address ever reaches a screen.

This module holds the geometry and the drawing. The basemap itself comes from map_tiles.py, which asks Home
Assistant's own `/api/map_tiles/raster` proxy; a card without one is drawn schematic, from the zones alone.

Refresh is driven by a mark, not a clock: `fingerprint` turns the tile's stored choices, the shown entities'
quantised places and states and a digest of the zones into a short hash, `extras` puts it in the tile's state as
`x.mk`, and the firmware folds it into the picture it wishes for. Nothing moves, nothing is drawn, nothing is
downloaded. The mark is deliberately independent of the frame, the look and the basemap choice: those are already
part of the wish (the atlas and the `dark` field), so putting them in the mark too would only cost pictures.
"""
import hashlib
import json
import math
from pathlib import Path
import re

# A map tile's own entity is a person; a person or a device tracker rides along as a companion (version 1).
MAP_DOMAINS = frozenset(('person', 'device_tracker'))
MAP_MAX_ENTITIES = 8
# 'fit' follows the people; the fixed steps mirror the Home Assistant map card, whose own default is 14.
MAP_ZOOMS = ('fit', '17', '15', '13', '11')
MAP_LABELS = ('names', 'initials', 'nothing')
MAP_BASEMAPS = ('auto', 'none')

TILE_PX = 256
# What one card render may ask the proxy for. The basemap is fetched a zoom step further out, and scaled up, until
# it fits this budget, so a full-page map costs no more requests than a small one (map_tiles.MAX_TILES).
MAX_TILES = 12
RASTER_MAX_ZOOM = 19
FIT_MAX_ZOOM = 17
FIT_MIN_ZOOM = 3
FIT_PADDING = 0.12
# With nothing on the map, the card sits over the home zone at a neighbourhood zoom and says there is no location.
HOME_ZOOM = 15
# The grid the movement mark quantises to, in metres: about a house's width, so a phone reporting a few metres of
# drift does not make the screen download a new picture (E4 measures this against a real phone).
MARK_METRES = 25
MARK_DEGREES = MARK_METRES / 111320.0

ENTITY = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+')
# Mercator cannot draw the poles; the same limit Leaflet and Home Assistant use.
MAX_LATITUDE = 85.05112878
# What a zone without one is worth, as Home Assistant's own default.
DEFAULT_RADIUS = 100.0
EARTH_METRES = 6371000.0
AWAY_STATES = ('not_home', 'away')
UNKNOWN_STATES = ('unavailable', 'unknown', '', None)


def supported(entity):
    """True for an entity id a map tile may show: a person or a device tracker."""
    return (isinstance(entity, str) and len(entity) <= 120 and ENTITY.fullmatch(entity) is not None
            and entity.split('.')[0] in MAP_DOMAINS)


def shown_entities(tile):
    """The entities this tile draws, in drawing and legend order: its own first, then its companions, each once.

    Bounded here as well as in validate_layout, so a stored list from a newer app cannot widen a render."""
    own = tile.get('entity')
    shown = [own] if supported(own) else []
    for entity in (tile.get('options') or {}).get('map') or ():
        if supported(entity) and entity not in shown:
            shown.append(entity)
    return shown[:MAP_MAX_ENTITIES]


# ----- Web Mercator, the projection Home Assistant's raster basemap is drawn in -----

def _world(zoom):
    """The width of the whole world at this zoom, in pixels."""
    return float(TILE_PX * (1 << int(zoom)))


def _norm(lat, lon):
    """(x, y) of a place in the unit square: 0 at the west and north edge, 1 at the east and south."""
    sin = math.sin(math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)


def _place(x, y):
    """The place a point of the unit square stands for."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))), x * 360.0 - 180.0


def _number(value):
    """A finite float, or None for anything else (a string, None, a NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coordinates(attributes):
    """(latitude, longitude) of an entity or a zone, or (None, None) when it has no usable place."""
    lat, lon = _number(attributes.get('latitude')), _number(attributes.get('longitude'))
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def metres_between(lat1, lon1, lat2, lon2):
    """The great-circle distance in metres, for whether someone stands inside a zone."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_METRES * math.asin(min(1.0, math.sqrt(a)))


class Viewport:
    """What a frame shows: the middle, the zoom the markers are drawn at, and the basemap tiles that cover it.

    `zoom` is the ground scale everything is projected at. `source_zoom` is the zoom the basemap is fetched at,
    never higher and stepped down until the tiles fit the render's budget; `scale` is how much those tiles are
    blown up to fill the frame. Keeping the two apart is what bounds the requests: a frame's tile count follows
    from its pixels, not from how far out the map is, so only fetching a softer basemap can bring it down.

    `offset` is where the middle of the map sits inside the frame, in pixels from the frame's own middle. The
    bottom of a card is the band the screen writes the tile's name in and the legend the card draws itself, so the
    map people are fitted into is shorter than the frame; the basemap still covers the whole frame, so the streets
    run on under both. Everything (the projection and the basemap's own origin) reads this one number, which is why
    the tiles that are fetched are always the tiles the markers are drawn on.
    """
    __slots__ = ('lat', 'lon', 'zoom', 'source_zoom', 'width', 'height', 'anchor', 'offset')

    def __init__(self, lat, lon, zoom, size, source_zoom=None, offset=(0.0, 0.0)):
        self.lat, self.lon, self.zoom = lat, lon, int(zoom)
        self.width, self.height = int(size[0]), int(size[1])
        self.source_zoom = self.zoom if source_zoom is None else int(source_zoom)
        self.anchor = None
        self.offset = (float(offset[0]), float(offset[1]))

    @property
    def scale(self):
        """How many frame pixels one basemap pixel covers: 1 at the native zoom, 2 one step out."""
        return 1 << (self.zoom - self.source_zoom)

    @property
    def source_size(self):
        """The frame in basemap pixels: what has to be stitched together and then blown up by `scale`."""
        return self.width / self.scale, self.height / self.scale

    def source_origin(self):
        """The basemap pixel at the frame's top left, at `source_zoom`; the same offset the projection uses."""
        x, y = _norm(self.lat, self.lon)
        world = _world(self.source_zoom)
        wide, tall = self.source_size
        return (x * world - wide / 2 - self.offset[0] / self.scale,
                y * world - tall / 2 - self.offset[1] / self.scale)

    def _grid(self):
        """(first column, last column, first row, last row) of the basemap mosaic, in unwrapped tile numbers."""
        left, top = self.source_origin()
        wide, tall = self.source_size
        return (math.floor(left / TILE_PX), math.floor((left + wide - 1e-9) / TILE_PX),
                math.floor(top / TILE_PX), math.floor((top + tall - 1e-9) / TILE_PX))

    def tile_places(self):
        """[((zoom, x, y), (left, top))] of every basemap tile, with its place in the mosaic, in reading order.

        x wraps the world the way a slippy map does, so the same tile can stand in two places; a row outside the
        map (over a pole) is left out and the render keeps its own background there."""
        x0, x1, y0, y1 = self._grid()
        span = 1 << self.source_zoom
        places = []
        for row in range(y0, y1 + 1):
            for column in range(x0, x1 + 1):
                if 0 <= row < span:
                    places.append(((self.source_zoom, column % span, row),
                                   ((column - x0) * TILE_PX, (row - y0) * TILE_PX)))
        return places

    def mosaic(self):
        """((width, height), (left, top, right, bottom)) of the stitched basemap and the frame's box inside it."""
        x0, x1, y0, y1 = self._grid()
        left, top = self.source_origin()
        wide, tall = self.source_size
        inset = (left - x0 * TILE_PX, top - y0 * TILE_PX)
        return (((x1 - x0 + 1) * TILE_PX, (y1 - y0 + 1) * TILE_PX),
                (inset[0], inset[1], inset[0] + wide, inset[1] + tall))

    def tile_keys(self):
        """(zoom, x, y) of every basemap tile the frame needs, each once, in reading order."""
        return list(dict.fromkeys(key for key, _ in self.tile_places()))

    @property
    def tiles(self):
        """How many basemap tiles this frame needs."""
        return len(self.tile_keys())


def project(lat, lon, view, size):
    """Where a place lands in a frame of `size`, in pixels from its top left; outside means off the card."""
    width, height = size
    x, y = _norm(lat, lon)
    cx, cy = _norm(view.lat, view.lon)
    world = _world(view.zoom)
    return width / 2.0 + view.offset[0] + (x - cx) * world, height / 2.0 + view.offset[1] + (y - cy) * world


def unproject(x, y, view, size):
    """The place a point of the frame stands on: `project` the other way round."""
    width, height = size
    cx, cy = _norm(view.lat, view.lon)
    world = _world(view.zoom)
    return _place(cx + (x - width / 2.0 - view.offset[0]) / world,
                  cy + (y - height / 2.0 - view.offset[1]) / world)


def _points_of(points):
    """(lat, lon) pairs from plain tuples or from Positions, leaving out what has no place."""
    found = []
    for item in points or ():
        lat = getattr(item, 'lat', None)
        lon = getattr(item, 'lon', None)
        if lat is None and lon is None and isinstance(item, (tuple, list)) and len(item) == 2:
            lat, lon = _number(item[0]), _number(item[1])
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            found.append((float(lat), float(lon)))
    return found


def _fit_zoom(box, size):
    """The closest zoom whose world still holds `box` inside a frame of `size`, within the card's own limits."""
    (x0, y0), (x1, y1) = box
    width, height = size
    for zoom in range(FIT_MAX_ZOOM, FIT_MIN_ZOOM - 1, -1):
        world = _world(zoom)
        if (x1 - x0) * world <= width and (y1 - y0) * world <= height:
            return zoom
    return FIT_MIN_ZOOM


def viewport(points, zones=(), zoom='fit', size=(320, 240), max_tiles=MAX_TILES, inner=None):
    """What the card shows: everyone with a place inside the frame, or a fixed zoom around the middle of them.

    Zones only join the fit when a shown entity stands in one, so a holiday zone on another continent cannot
    flatten the map. With nobody anywhere the frame sits over the home zone.

    `inner` is the part of the frame that is really map: the bottom carries the band the screen writes the tile's
    name in and the legend the card draws. The fit is made for that rectangle and its middle is put in the middle
    of it, so nobody is fitted into room they are then drawn out of. The basemap keeps covering the whole frame.
    """
    inner = (int(size[0]), int(size[1])) if inner is None else (max(1, int(inner[0])), max(1, int(inner[1])))
    offset = ((inner[0] - int(size[0])) / 2.0, (inner[1] - int(size[1])) / 2.0)
    spots = _points_of(points)
    fixed = None
    if zoom != 'fit':
        try:
            fixed = max(0, min(RASTER_MAX_ZOOM, int(zoom)))
        except (TypeError, ValueError):
            fixed = None
    if not spots:
        home = home_zone(zones)
        centre = (home.lat, home.lon) if home else (0.0, 0.0)
        view = Viewport(centre[0], centre[1], fixed if fixed is not None else HOME_ZOOM, size, offset=offset)
    elif fixed is not None:
        view = Viewport(sum(lat for lat, _ in spots) / len(spots), sum(lon for _, lon in spots) / len(spots), fixed,
                        size, offset=offset)
    else:
        corners = [_norm(lat, lon) for lat, lon in spots]
        for zone in zones or ():
            if any(metres_between(zone.lat, zone.lon, lat, lon) <= zone.radius for lat, lon in spots):
                # The zone's own circle, so the ring it is drawn as stays on the card.
                degrees = zone.radius / 111320.0
                wide = degrees / max(0.01, math.cos(math.radians(zone.lat)))
                corners += [_norm(max(-MAX_LATITUDE, min(MAX_LATITUDE, zone.lat + sign * degrees)), zone.lon + other * wide)
                            for sign in (-1, 1) for other in (-1, 1)]
        x0, x1 = min(x for x, _ in corners), max(x for x, _ in corners)
        y0, y1 = min(y for _, y in corners), max(y for _, y in corners)
        pad_x, pad_y = (x1 - x0) * FIT_PADDING, (y1 - y0) * FIT_PADDING
        box = ((x0 - pad_x, y0 - pad_y), (x1 + pad_x, y1 + pad_y))
        middle = _place((box[0][0] + box[1][0]) / 2, (box[0][1] + box[1][1]) / 2)
        view = Viewport(middle[0], middle[1], _fit_zoom(box, inner), size, offset=offset)
    # The basemap a step further out until its tiles fit the budget; the ground the card shows does not change.
    budget = max(1, int(max_tiles))
    while view.source_zoom > 0 and view.tiles > budget:
        view.source_zoom -= 1
    return view


# ----- What is on the map -----

class Zone:
    """A Home Assistant zone with a place: its ring and its name on the card."""
    __slots__ = ('entity', 'name', 'lat', 'lon', 'radius', 'passive')

    def __init__(self, entity, name, lat, lon, radius, passive=False):
        self.entity, self.name = entity, name
        self.lat, self.lon, self.radius, self.passive = lat, lon, radius, passive


class Position:
    """One shown entity on the card: where it is, how well that is known, and what to call it.

    `kind` is 'point' for coordinates of its own, 'zone' for a zone it says it is in (drawn hollow, because the
    place is the zone's and not theirs), 'away' for somewhere off the map, and 'unknown' for unavailable.
    """
    __slots__ = ('entity', 'name', 'state', 'kind', 'lat', 'lon', 'zone')

    def __init__(self, entity, name, state, kind, lat=None, lon=None, zone=None):
        self.entity, self.name, self.state, self.kind = entity, name, state, kind
        self.lat, self.lon, self.zone = lat, lon, zone

    @property
    def placed(self):
        return self.lat is not None and self.lon is not None


def zones(states):
    """Every zone Home Assistant has a place for, in entity order."""
    found = []
    for entity, state in sorted((states or {}).items()):
        if not isinstance(entity, str) or not entity.startswith('zone.') or not isinstance(state, dict):
            continue
        attributes = state.get('attributes') or {}
        lat, lon = _coordinates(attributes)
        if lat is None:
            continue
        radius = _number(attributes.get('radius'))
        found.append(Zone(entity, str(attributes.get('friendly_name') or entity.split('.', 1)[1].replace('_', ' ')),
                          lat, lon, DEFAULT_RADIUS if radius is None or radius <= 0 else radius,
                          bool(attributes.get('passive'))))
    return found


def home_zone(found):
    """The home zone, the anchor for a card with nothing on it; None when Home Assistant has none."""
    return next((zone for zone in found or () if zone.entity == 'zone.home'), None)


def _zone_named(found, state):
    """The zone an entity's state names, by the name Home Assistant shows or by its own id."""
    if not isinstance(state, str) or not state:
        return None
    wanted = state.strip().casefold()
    for zone in found:
        if zone.name.strip().casefold() == wanted or zone.entity.split('.', 1)[1].replace('_', ' ') == wanted:
            return zone
    return next((zone for zone in found if zone.entity == 'zone.home'), None) if wanted == 'home' else None


def positions(entities, states, found_zones=None):
    """Where each shown entity is, in the order it is drawn."""
    near = zones(states) if found_zones is None else found_zones
    out = []
    for entity in entities:
        state = (states or {}).get(entity)
        attributes = (state or {}).get('attributes') or {}
        name = str(attributes.get('friendly_name') or entity.split('.', 1)[-1].replace('_', ' '))
        value = (state or {}).get('state')
        lat, lon = _coordinates(attributes)
        if lat is not None:
            out.append(Position(entity, name, value, 'point', lat, lon))
            continue
        if value in UNKNOWN_STATES or state is None:
            out.append(Position(entity, name, value, 'unknown'))
            continue
        zone = None if str(value).casefold() in AWAY_STATES else _zone_named(near, value)
        if zone is not None:
            out.append(Position(entity, name, value, 'zone', zone.lat, zone.lon, zone.entity))
        else:
            out.append(Position(entity, name, value, 'away'))
    return out


def marker_color(position):
    """Which paint a marker takes: at home, in another zone, away, or unavailable. Takes a Position or a state."""
    kind = getattr(position, 'kind', None)
    state = getattr(position, 'state', position)
    if kind == 'unknown' or state in UNKNOWN_STATES:
        return 'unknown'
    if kind == 'away' or (isinstance(state, str) and state.casefold() in AWAY_STATES):
        return 'away'
    return 'home' if isinstance(state, str) and state.casefold() == 'home' else 'zone'


# ----- The movement mark -----

def cell(lat, lon):
    """The MARK_METRES cell a place falls in, longitude scaled by the latitude so a cell is about as wide as tall."""
    if lat is None or lon is None:
        return None
    row = math.floor(lat / MARK_DEGREES)
    narrow = max(0.01, math.cos(math.radians((row + 0.5) * MARK_DEGREES)))
    return [row, math.floor(lon * narrow / MARK_DEGREES)]


def fingerprint(tile, states):
    """The tile's movement mark: what its picture is made of, with places quantised to a cell of MARK_METRES.

    Frame independent on purpose (see the module note): only the tile's own entity, its stored choices, the shown
    entities' cells and states, and the zones go in.
    """
    options = tile.get('options') or {}
    shown = shown_entities(tile)
    near = zones(states)
    body = [tile.get('entity'),
            [options.get(key) for key in ('map', 'zoom', 'labels', 'basemap', 'overlay')],
            [[item.entity, item.kind, item.state, cell(item.lat, item.lon)] for item in positions(shown, states, near)],
            [[zone.entity, zone.name, cell(zone.lat, zone.lon), round(zone.radius)] for zone in near]]
    raw = json.dumps(body, separators=(',', ':'), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def extras(tile, states):
    """The tile's own extras: the movement mark as `x.mk`, or None for a tile that is no map."""
    if (tile.get('options') or {}).get('display') != 'map':
        return None
    return {'mk': fingerprint(tile, states)}


# ----- The look -----
# Every value here is a role of components/smart_display/theme.h, light and dark, so the card matches the rest of the
# screen in both looks and no colour is invented outside that table. The caller may hand `render` its own dict.
PALETTES = {
    False: {'ground': 0xFFFFFF,     # CARD: the schematic background, and what shows where a basemap tile is missing
            'wash': 0xE7E7E7,       # PAGE: the schematic land
            'line': 0xDDDDDD,       # LINE: the schematic grid and the scale bar
            'ink': 0x1B1B1B,        # INK: names and the legend
            'muted': 0x616161,      # MUTED: states, the attribution, the count line
            'halo': 0xFFFFFF,       # CARD: the outline that keeps a label legible over streets
            'zone': 0xD5EEFC,       # ACCENT_TINT: a zone's circle
            'zone_line': 0x009FE3,  # ACCENT: its edge
            'home': 0x009FE3,       # ACCENT: at home
            'in_zone': 0x0B6E99,    # ACCENT_ICON: in another zone
            'away': 0x46525E,       # SLATE: somewhere else
            'unknown': 0x9E9E9E,    # OFF: unavailable
            'pill': 0xFFFFFF},      # CARD: behind the attribution
    True: {'ground': 0x1A1A1A, 'wash': 0x000000, 'line': 0x292929, 'ink': 0xDADADA, 'muted': 0x999999,
           'halo': 0x1A1A1A, 'zone': 0x12384A, 'zone_line': 0x0A93D2, 'home': 0x0A93D2, 'in_zone': 0x6CCBF5,
           'away': 0x9AA6B2, 'unknown': 0x787878, 'pill': 0x1A1A1A},
}
MARKER_PAINTS = {'home': 'home', 'zone': 'in_zone', 'away': 'away', 'unknown': 'unknown'}

# Home Assistant's raster style has one flavour, so the dark look tones it instead of asking for another basemap:
# what is left of its colour, and how far the result is blended towards the card's own dark ground. Streets then read
# as texture rather than glare and white labels stay legible (plan E3 compares the two on a panel).
DARK_SATURATION = 0.35
DARK_BLEND = 0.55

# The bottom band the screen writes the tile's name in (runtime_tiles.h render_camera_card puts one title line there).
# Its exact height is the board's font, which the add-on does not know, so the card keeps a little more room than the
# smallest of them needs and draws nothing of its own inside it.
BAND_SHARE = 0.18
BAND_MIN, BAND_MAX = 16, 36

ATTRIBUTION = '© OpenStreetMap'
# The licence is never traded away for a thumbnail: a frame too short to carry a legible pill with its padding does
# not use the basemap at all and is drawn schematic instead (plan 3.5).
ATTRIBUTION_MIN_TEXT = 9
ATTRIBUTION_MIN_HEIGHT = 60

LEGEND_ROWS = 4
# The frame a legend needs at all, and the frame that carries every row of it (a full page).
LEGEND_MIN = (120, 150)
LEGEND_FULL = (480, 320)
LABEL_MIN = 100      # under this the markers carry no label: there is no room to read one
SCALE_MIN = (160, 120)
# The distances a scale bar may show, in metres.
SCALE_STEPS = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000)
# One basemap pixel at the equator, at zoom 0 (the Web Mercator constant Leaflet uses).
EQUATOR_METRES = 156543.03392

_FONTS = {}


def _font_files(bold):
    """Where a text font may be found, best first: the repository's own Roboto, then a system one.

    In the add-on's image neither is there (its Dockerfile copies the code and the translations, not the fonts), so
    the last word is Pillow's own built-in font, which is always present and scales.
    """
    here = Path(__file__).resolve().parent
    name = 'Roboto-500.ttf' if bold else 'Roboto-400.ttf'
    system = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    return [here.parents[1] / 'fonts' / name, here / 'fonts' / name,
            Path('/usr/share/fonts/truetype/dejavu') / system]


def _font(size, bold=False):
    """A text font of `size` pixels, loaded once."""
    key = (max(6, int(size)), bool(bold))
    if key not in _FONTS:
        from PIL import ImageFont
        for path in _font_files(key[1]):
            try:
                _FONTS[key] = ImageFont.truetype(str(path), key[0])
                break
            except OSError:
                continue
        else:
            _FONTS[key] = ImageFont.load_default(size=key[0])
    return _FONTS[key]


# Measured text boxes, by (font, text). Measuring is the same arithmetic every time and a card measures the same
# names several times over (a marker's label and its legend row), so this keeps a bounded memo of them. The font is
# named by identity, which is safe because `_FONTS` keeps every font it ever loaded, so no id is ever reused.
_MEASURED = {}
_MEASURED_KEPT = 512


def _text_size(text, font):
    """(width, height) of a piece of text in a font, measured the way it will be drawn."""
    key = (id(font), text)
    found = _MEASURED.get(key)
    if found is None:
        left, top, right, bottom = font.getbbox(text)
        found = _MEASURED[key] = (right - left, bottom - top)
        if len(_MEASURED) > _MEASURED_KEPT:
            _MEASURED.clear()
            _MEASURED[key] = found
    return found


def _rgb(value):
    """A colour of the palette as (r, g, b)."""
    if isinstance(value, (tuple, list)):
        return tuple(int(part) for part in value[:3])
    return ((int(value) >> 16) & 255, (int(value) >> 8) & 255, int(value) & 255)


def palette(dark=False, colors=None):
    """The colours a render draws with: the theme's own for this look, with anything the caller names on top."""
    found = {key: _rgb(value) for key, value in PALETTES[bool(dark)].items()}
    for key, value in (colors or {}).items():
        if key in found:
            found[key] = _rgb(value)
    return found


# ----- What goes where in the frame -----

def band_of(size, options):
    """The pixels at the bottom the screen writes the tile's name in, 0 when the tile shows no name on its picture."""
    if (options or {}).get('overlay', 'name') == 'none':
        return 0
    return min(BAND_MAX, max(BAND_MIN, round(int(size[1]) * BAND_SHARE)))


def wants_basemap(tile, size):
    """Whether this card is drawn on Home Assistant's basemap, so the caller knows whether to fetch tiles at all.

    False for `basemap: none`, and false for a frame too short to carry a legible attribution pill: the licence is a
    condition of using the tiles, so a card that cannot show it does not use them (plan 3.5).
    """
    options = tile.get('options') or {}
    return options.get('basemap', 'auto') != 'none' and int(size[1]) >= ATTRIBUTION_MIN_HEIGHT


def metres_per_pixel(lat, zoom):
    """How much ground one frame pixel covers, for a zone's circle and the scale bar."""
    return EQUATOR_METRES * math.cos(math.radians(lat)) / (1 << int(zoom))


class Marker:
    """One entity on the map: where its dot lands, what paint it takes, and the label beside it."""
    __slots__ = ('entity', 'name', 'label', 'x', 'y', 'paint', 'hollow', 'radius', 'label_box')

    def __init__(self, entity, name, label, x, y, paint, hollow, radius, label_box=None):
        self.entity, self.name, self.label = entity, name, label
        self.x, self.y, self.paint, self.hollow, self.radius = x, y, paint, hollow, radius
        self.label_box = label_box


class Ring:
    """One zone's circle on the map."""
    __slots__ = ('entity', 'name', 'x', 'y', 'radius', 'passive')

    def __init__(self, entity, name, x, y, radius, passive):
        self.entity, self.name, self.x, self.y, self.radius, self.passive = entity, name, x, y, radius, passive


class Frame:
    """Everything the render draws, worked out before a pixel is touched, so the arithmetic can be tested on its own.

    `content` is the frame without the band the screen writes the tile's name in; `map_area` is what is left of it
    above the legend, and is where the markers, their labels, the scale bar and the attribution stand.
    """
    __slots__ = ('size', 'view', 'band', 'content', 'map_area', 'basemap', 'attribution', 'attribution_text',
                 'markers', 'rings', 'legend', 'more', 'columns', 'note', 'away', 'unavailable', 'scale_bar',
                 'labels', 'row')

    def __init__(self, size, view, band, content):
        self.size, self.view, self.band, self.content = size, view, band, content
        self.map_area, self.row = content, 0
        self.basemap = False
        self.attribution, self.attribution_text = None, 0
        self.markers, self.rings, self.legend = [], [], []
        self.more, self.columns = 0, 1
        self.note, self.away, self.unavailable = None, 0, 0
        self.scale_bar, self.labels = None, 'nothing'


def initials(name):
    """What a name shrinks to when it does not fit: "Robin de Vries" becomes "RV"."""
    parts = [word for word in re.split(r'[\s\-]+', str(name or '')) if word]
    return ''.join(word[0].upper() for word in parts[:2]) or '?'


def _legend_shape(size, count):
    """(rows shown, columns) of the legend for this frame; no rows at all on a frame with no room for them."""
    width, height = int(size[0]), int(size[1])
    if width < LEGEND_MIN[0] or height < LEGEND_MIN[1] or not count:
        return 0, 1
    if width >= LEGEND_FULL[0] and height >= LEGEND_FULL[1]:
        # A full page shows every row, in two columns when it is much wider than tall (docs/RESPONSIVE.md's shape rule).
        return count, 2 if width * 2 >= height * 3 else 1
    return min(LEGEND_ROWS, count), 1


def legend_metrics(height):
    """(the text size of a legend row, the height of a row): one rule, so `frame` and `render` place the same rows."""
    text = max(ATTRIBUTION_MIN_TEXT, min(14, round(int(height) / 24)))
    return text, max(text + 4, 13)


def _scale_bar(view, size, room):
    """(pixels, metres) of the scale bar, or None on a frame with no room for one."""
    width = int(size[0])
    if width < SCALE_MIN[0] or room < SCALE_MIN[1]:
        return None
    per_pixel = metres_per_pixel(view.lat, view.zoom)
    for metres in reversed(SCALE_STEPS):
        pixels = metres / per_pixel
        if 40 <= pixels <= width * 0.35:
            return int(round(pixels)), metres
    return None


def reserved(size, tile, found):
    """(band, legend block, legend rows, legend columns) the bottom of a frame of `size` is not map.

    `band` is where the screen writes the tile's name, `block` what the card's own legend and count line take. Both
    `view_of` and `frame` work from this, so the ground that is fitted is the ground that is drawn on.
    """
    width, height = int(size[0]), int(size[1])
    band = band_of(size, (tile.get('options') or {}))
    room = max(1, height - band)
    rows, columns = _legend_shape((width, height), len(found))
    notes = 1 if has_notes(found, rows) else 0
    _, row = legend_metrics(height)
    block = row * (math.ceil(rows / columns) + notes) + (4 if rows or notes else 0) if (rows or notes) else 0
    return band, min(block, int(room * 0.55)), rows, columns


def has_notes(found, rows):
    """Whether the card writes a line under the legend: nobody anywhere, somebody off the map, or rows that did not
    fit. One rule, so the room `reserved` keeps is the room the render draws in."""
    return (not any(item.placed for item in found)
            or any(item.kind in ('away', 'unknown') for item in found)
            or len(found) > rows > 0)


def map_area(size, tile, states, found=None):
    """(width, height) of the part of a frame that is really map: the frame without the band and the legend."""
    width, height = int(size[0]), int(size[1])
    if found is None:
        found = positions(shown_entities(tile), states, zones(states))
    band, block, _rows, _columns = reserved((width, height), tile, found)
    return width, max(1, max(1, height - band) - block)


def view_of(size, tile, states, found=None, near=None):
    """The viewport a frame of `size` shows for this tile: what `frame` draws in and what the basemap is fetched for.

    One place, so the tiles that are fetched are exactly the tiles the card is then drawn on (server.map_basemap).
    """
    if near is None:
        near = zones(states)
    if found is None:
        found = positions(shown_entities(tile), states, near)
    placed = [item for item in found if item.placed]
    return viewport(placed, near, (tile.get('options') or {}).get('zoom', 'fit'),
                    (int(size[0]), int(size[1])), MAX_TILES, inner=map_area(size, tile, states, found))


def frame(size, tile, states, dark=False, basemap=False):
    """Where everything lands in a frame of `size`: the markers, the zone circles, the legend and the attribution.

    `basemap` says whether a basemap image is on offer. Whether it is used is this function's decision, not the
    caller's: a frame too short for a legible attribution pill is drawn schematic (wants_basemap).
    """
    width, height = int(size[0]), int(size[1])
    options = tile.get('options') or {}
    near = zones(states)
    found = positions(shown_entities(tile), states, near)
    view = view_of((width, height), tile, states, found, near)
    band, block, rows, columns = reserved((width, height), tile, found)
    inner = (0, 0, width, max(1, height - band))
    out = Frame((width, height), view, band, inner)
    out.basemap = bool(basemap) and wants_basemap(tile, size)
    out.labels = options.get('labels', 'names')
    out.away = sum(1 for item in found if item.kind == 'away')
    out.unavailable = sum(1 for item in found if item.kind == 'unknown')
    if not any(item.placed for item in found):
        out.note = 'no_location'

    # What the legend takes at the bottom is no longer map (reserved), so nothing of the map is drawn under it.
    out.columns = columns
    out.legend = [(item.name, MARKER_PAINTS[marker_color(item)], item.state) for item in found[:rows]]
    out.more = len(found) - rows if rows else 0
    _, out.row = legend_metrics(height)
    out.map_area = (0, 0, width, max(1, inner[3] - block))
    floor = out.map_area[3]

    # The zone circles, drawn under the markers and in the same projection.
    per_pixel = metres_per_pixel(view.lat, view.zoom)
    for zone in near:
        x, y = project(zone.lat, zone.lon, view, (width, height))
        radius = zone.radius / per_pixel
        if x + radius < -width or x - radius > 2 * width or y + radius < -height or y - radius > 2 * height:
            continue
        out.rings.append(Ring(zone.entity, zone.name, x, y, radius, zone.passive))

    dot = max(3, min(9, round(min(width, floor) / 34)))
    label_font = _font(_label_text(height)) if min(width, height) >= LABEL_MIN and out.labels != 'nothing' else None
    for item in found:
        if not item.placed:
            continue
        x, y = project(item.lat, item.lon, view, (width, height))
        x = min(max(x, dot + 1), max(dot + 1, width - dot - 1))
        y = min(max(y, dot + 1), max(dot + 1, floor - dot - 1))
        marker = Marker(item.entity, item.name, '', x, y, MARKER_PAINTS[marker_color(item)], item.kind == 'zone', dot)
        if label_font is not None:
            marker.label = initials(item.name) if out.labels == 'initials' else str(item.name)
            marker.label_box = _label_box(marker, label_font, width, floor)
            if marker.label_box is None and out.labels != 'initials':
                # A name that does not fit degrades to initials, which is a render decision and never a stored one.
                marker.label = initials(item.name)
                marker.label_box = _label_box(marker, label_font, width, floor)
        out.markers.append(marker)

    out.scale_bar = _scale_bar(view, (width, height), floor)
    if out.basemap:
        # The licence goes with the tiles, so the pill comes first: it is written as large as the card affords and
        # made smaller down to the legible floor to fit. A frame that cannot carry even that goes without streets
        # rather than without the attribution (plan 3.5).
        pad, right, bottom = 3, width - 4, floor - 3
        out.basemap, out.attribution = False, None
        for text in range(max(ATTRIBUTION_MIN_TEXT, min(13, round(height / 28))), ATTRIBUTION_MIN_TEXT - 1, -1):
            wide, tall = _text_size(ATTRIBUTION, _font(text))
            left, top = right - (wide + 2 * pad + 2), bottom - (tall + 2 * pad)
            if left >= 2 and top >= 2:
                out.basemap, out.attribution, out.attribution_text = True, (left, top, right, bottom), text
                break
    return out


def _label_text(height):
    """The size a marker's label is written at: never under the legible floor, never larger than a line needs."""
    return max(ATTRIBUTION_MIN_TEXT, min(15, round(int(height) / 22)))


def _label_box(marker, font, width, height):
    """Where a marker's label fits, beside it and inside the card, or None when it fits nowhere."""
    wide, tall = _text_size(marker.label, font)
    gap = marker.radius + 3
    for left, top in ((marker.x + gap, marker.y - tall / 2), (marker.x - gap - wide, marker.y - tall / 2),
                      (marker.x - wide / 2, marker.y + gap), (marker.x - wide / 2, marker.y - gap - tall)):
        if 0 <= left and left + wide <= width and 0 <= top and top + tall <= height:
            return (left, top, left + wide, top + tall)
    return None


# ----- The picture -----

def _tone(image, colours):
    """Home Assistant's raster basemap in the dark look: what is left of its colour, blended towards the dark ground.

    Desaturated and darkened, never inverted, so a street still reads as a street and a white label stays legible.
    """
    from PIL import Image, ImageEnhance
    ground = Image.new('RGB', image.size, colours['ground'])
    return Image.blend(ImageEnhance.Color(image).enhance(DARK_SATURATION), ground, DARK_BLEND)


def _schematic(canvas, colours):
    """The background of a card without basemap tiles: the themed wash with a quiet grid, so it reads as a map."""
    from PIL import ImageDraw
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width - 1, height - 1), fill=colours['wash'])
    step = max(16, round(min(width, height) / 8))
    for x in range(step, width, step):
        draw.line((x, 0, x, height), fill=colours['line'])
    for y in range(step, height, step):
        draw.line((0, y, width, y), fill=colours['line'])


def _write(canvas, place, text, font, fill, halo=None):
    """Text, with a one pixel outline of the card's own colour when it stands over streets.

    The outline is one glyph render grown by a pixel, not Pillow's `stroke_width`, which renders the text a second
    time and costs four times as much; a busy full page draws about twenty pieces of text (plan E5).
    """
    from PIL import Image, ImageFilter
    letters = _letters(text, font)
    if letters is None:
        return
    x, y = int(round(place[0])), int(round(place[1]))
    if halo is not None:
        padded = Image.new('L', (letters.width + 2, letters.height + 2))
        padded.paste(letters, (1, 1))
        canvas.paste(halo, (x - 1, y - 1), padded.filter(ImageFilter.MaxFilter(3)))
    canvas.paste(fill, (x, y), letters)


def _letters(text, font):
    """The ink of a piece of text as an 'L' mask whose top left is the ink's own, or None for nothing to draw.

    `getmask2` already gives the mask cropped to the ink, so this is one glyph render and no measuring at all; that
    render is the whole cost of drawing text (plan E5).
    """
    from PIL import Image
    mask, _offset = font.getmask2(text, mode='L')
    if not mask.size[0] or not mask.size[1]:
        return None
    return Image.frombytes('L', mask.size, bytes(mask))


def render(size, tile, states, colors=None, dark=False, basemap_image=None, cache=None, words=None):
    """The whole card as a `PIL.Image` in RGB at exactly `size`: basemap, zones, markers, labels, legend, scale bar,
    the count line and the attribution. The screen places this bitmap and writes the tile's name in the bottom band.

    `basemap_image` is what map_tiles.TileSource.image gave, already cropped to `size`, or None for a schematic card.
    `cache` is the caller's render cache, keyed on the movement mark, the frame, the look and the basemap.

    A basemap that came back with a hole in it (`info['map_missing']`) makes the card provisional: it is drawn, and
    it says so in `info['map_provisional']`, but it is kept in no cache. The movement mark has not changed, so
    nothing would ever ask for that card again; leaving it out of the caches means the next load tries those tiles
    once more and the streets come back by themselves.
    """
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    key = (fingerprint(tile, states), width, height, bool(dark), basemap_image is not None,
           tuple(sorted((colors or {}).items())))
    if cache is not None and key in cache:
        return cache[key]
    from PIL import Image, ImageDraw
    colours = palette(dark, colors)
    say = words if words is not None else _screen_words
    out = frame((width, height), tile, states, dark=dark, basemap=basemap_image is not None)
    canvas = Image.new('RGB', (width, height), colours['ground'])
    if out.basemap and basemap_image is not None:
        streets = basemap_image if basemap_image.size == (width, height) else \
            basemap_image.resize((width, height), Image.Resampling.BICUBIC)
        canvas.paste(streets.convert('RGB'), mask=streets.getchannel('A') if streets.mode == 'RGBA' else None)
        if dark:
            canvas = _tone(canvas, colours)
    else:
        _schematic(canvas, colours)

    # The zone circles on a layer of their own, so their fill stays translucent over the streets.
    if out.rings:
        rings = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        pen = ImageDraw.Draw(rings)
        for ring in out.rings:
            box = (ring.x - ring.radius, ring.y - ring.radius, ring.x + ring.radius, ring.y + ring.radius)
            pen.ellipse(box, fill=colours['zone'] + (90 if ring.passive else 130,),
                        outline=colours['zone_line'] + (110 if ring.passive else 190,), width=1)
        canvas = Image.alpha_composite(canvas.convert('RGBA'), rings).convert('RGB')

    draw = ImageDraw.Draw(canvas)
    for marker in out.markers:
        paint, radius = colours[marker.paint], marker.radius
        box = (marker.x - radius, marker.y - radius, marker.x + radius, marker.y + radius)
        # A hollow marker says the place is the zone's and not the entity's own (plan 3.9).
        draw.ellipse(box, fill=None if marker.hollow else paint, outline=paint, width=max(2, radius // 3))
        if not marker.hollow:
            draw.ellipse(box, outline=colours['halo'], width=1)
    label_font = _font(_label_text(height))
    for marker in out.markers:
        if marker.label and marker.label_box:
            _write(canvas, marker.label_box[:2], marker.label, label_font, colours['ink'], colours['halo'])

    if out.scale_bar:
        _draw_scale(canvas, out, colours)
    _draw_legend(canvas, out, colours, say)
    if out.attribution:
        _draw_attribution(canvas, out, colours)
    canvas.info['map_provisional'] = provisional = bool(getattr(basemap_image, 'info', {}).get('map_missing'))
    if cache is not None and not provisional:
        cache[key] = canvas
    return canvas


def _screen_words(key, **params):
    """The card's own words in the screens' language; plain English when the translations are not at hand."""
    try:
        import i18n
        return i18n.screen_t(f'screen.map.{key}', **params)
    except Exception:
        return {'no_location': 'No location', 'away': '{n} away', 'unavailable': 'Unavailable',
                'more': '+{n} more'}[key].replace('{n}', str(params.get('n', '')))


def _draw_scale(canvas, out, colours):
    """The bar that says how much ground the card covers, bottom left of the map area."""
    from PIL import ImageDraw
    pixels, metres = out.scale_bar
    font = _font(max(ATTRIBUTION_MIN_TEXT, min(12, round(out.size[1] / 30))))
    text = f'{metres} m' if metres < 1000 else f'{metres // 1000} km'
    bottom, left = out.map_area[3] - 5, 6
    draw = ImageDraw.Draw(canvas)
    draw.line((left, bottom, left + pixels, bottom), fill=colours['muted'], width=2)
    draw.line((left, bottom - 4, left, bottom), fill=colours['muted'], width=2)
    draw.line((left + pixels, bottom - 4, left + pixels, bottom), fill=colours['muted'], width=2)
    _write(canvas, (left, bottom - 7 - _text_size(text, font)[1]), text, font, colours['muted'], colours['halo'])


def _draw_legend(canvas, out, colours, say):
    """The rows under the map: who is on it, what state they are in, and how many of them did not fit.

    Over streets the block sits on one translucent panel rather than every row carrying its own outline: one composite
    instead of a second glyph render per row, and easier to read.
    """
    from PIL import Image, ImageDraw
    lines, notes = list(out.legend), []
    if out.note == 'no_location':
        notes.append(say('no_location'))
    if out.away:
        notes.append(say('away', n=out.away))
    if out.unavailable:
        notes.append(say('unavailable'))
    if out.more:
        notes.append(say('more', n=out.more))
    if not lines and not notes:
        return
    text, row = legend_metrics(out.size[1])
    font = _font(text)
    per_column = math.ceil(len(lines) / out.columns) if lines else 0
    rows = per_column + (1 if notes else 0)
    dot = max(3, font.size // 3)
    column_w = max(60, (out.size[0] - 12) // out.columns)
    top = max(2, out.content[3] - 2 - row * rows)
    if out.basemap:
        panel = Image.new('RGBA', (out.size[0], min(out.content[3] - top, row * rows + 4)), colours['ground'] + (165,))
        canvas.paste(Image.alpha_composite(canvas.crop((0, top - 2, out.size[0], top - 2 + panel.height)).convert('RGBA'),
                                           panel).convert('RGB'), (0, top - 2))
    draw = ImageDraw.Draw(canvas)
    for index, (name, paint, _state) in enumerate(lines):
        column, line = divmod(index, per_column)
        x, y = 6 + column * column_w, top + line * row
        if y + row > out.content[3]:
            break
        draw.ellipse((x, y + row // 2 - dot, x + 2 * dot, y + row // 2 + dot), fill=colours[paint])
        label = _fitted(name, font, column_w - 2 * dot - 14)
        if label:
            _write(canvas, (x + 2 * dot + 5, y + (row - _text_size(label, font)[1]) // 2), label, font, colours['ink'])
    if notes:
        text = _fitted('  '.join(notes), font, out.size[0] - 12)
        y = min(out.content[3] - row, top + row * per_column)
        if text:
            _write(canvas, (6, y + 1), text, font, colours['muted'], None if out.basemap else colours['halo'])


def _fitted(text, font, room):
    """`text`, shortened with a full stop until it fits `room` pixels."""
    text = str(text)
    if room <= 0:
        return ''
    if _text_size(text, font)[0] <= room:
        return text
    for length in range(len(text) - 1, 0, -1):
        cut = text[:length].rstrip() + '.'
        if _text_size(cut, font)[0] <= room:
            return cut
    return ''


def _draw_attribution(canvas, out, colours):
    """The licence the basemap comes with: a small rounded pill, bottom right, above the band with the tile's name."""
    from PIL import Image, ImageDraw
    left, top, right, bottom = (int(round(value)) for value in out.attribution)
    pill = Image.new('RGBA', (right - left, bottom - top), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((0, 0, right - left - 1, bottom - top - 1),
                                           radius=max(2, (bottom - top) // 3), fill=colours['pill'] + (190,))
    canvas.paste(Image.alpha_composite(canvas.crop((left, top, right, bottom)).convert('RGBA'), pill).convert('RGB'),
                 (left, top))
    font = _font(out.attribution_text)
    _write(canvas, (left + 4, top + 3), ATTRIBUTION, font, colours['muted'])
