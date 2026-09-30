"""The basemap of a map card, through Home Assistant's own tile proxy (app 0.4.24).

Home Assistant Core carries a `map_tiles` integration: a websocket command that hands its frontend a short-lived
token, and `/api/map_tiles/raster/{z}/{x}/{y}.png` that fetches OpenStreetMap tiles with Home Assistant's own
identification and caches them for seven days. This add-on rides on exactly that. It never contacts a tile server
itself, it sends no entity id and no name with a tile request (a request is a zoom and two whole numbers), and it
caches what it got so a page that reloads for its camera does not fetch the same streets again.

Every call is injected (`token_of` and `fetch_tile` come from server.HomeAssistant), so the tests drive this
without a network and the transport stays in one place.

When Home Assistant cannot answer, nothing breaks: the source goes quiet for UNAVAILABLE_COOLDOWN, says so once,
and the card is drawn schematic from the zones alone, which is what `basemap: none` looks like anyway.
"""
from collections import OrderedDict
import asyncio
import io
import logging
import re
import time

from map_card import MAX_TILES, RASTER_MAX_ZOOM, TILE_PX

LOG = logging.getLogger(__name__)

# Under Home Assistant's own API base, as `camera_proxy` and `image_proxy` are (server.HomeAssistant.base).
PATH = '/map_tiles/raster'
MAX_CONCURRENT = 4
# Home Assistant keeps its own tiles this long (map_tiles.const.TILE_TTL); ours are no fresher than that.
TILE_TTL = 7 * 24 * 60 * 60
CACHE_BYTES = 16 * 1024 * 1024
# Home Assistant rotates the token every 30 minutes, so a token is replaced before it can go stale under a render.
TOKEN_REFRESH = 25 * 60
# How long a Home Assistant that has no tiles for us is left alone: an older one without the integration, a
# refused command, or one that is away.
UNAVAILABLE_COOLDOWN = 600
FETCH_SECONDS = 10
MAX_TILE_BYTES = 512 * 1024
TOKEN = re.compile(r'[A-Za-z0-9_\-.]{1,128}')


class Unauthorized(Exception):
    """The proxy refused the token (401 or 403): it is worth fetching a new one and trying again, once."""


class TileSource:
    """The basemap tiles of the whole add-on: one token, one cache, one cooldown, shared by every screen.

    `token_of()` is an awaitable giving the token; `fetch_tile(z, x, y, token)` an awaitable giving the PNG bytes
    and raising `Unauthorized` when the token was refused. Both are server.HomeAssistant's.
    """

    def __init__(self, token_of, fetch_tile, clock=time.monotonic):
        self.token_of, self.fetch_tile, self.clock = token_of, fetch_tile, clock
        self._token, self._token_at = None, 0.0
        self._cache = OrderedDict()   # (zoom, x, y) -> (raw, the moment it was fetched)
        self._bytes = 0
        self._quiet_until = 0.0
        self._said = False

    # ----- what there is right now -----

    def available(self):
        """Whether it is worth asking Home Assistant for tiles at all."""
        return self.clock() >= self._quiet_until

    @property
    def bytes_used(self):
        """What the tile cache holds, which never passes CACHE_BYTES."""
        return self._bytes

    def cached_keys(self):
        """The tiles in the cache, least recently used first."""
        return list(self._cache)

    # ----- the token -----

    async def token(self, force=False):
        """The basemap token, fetched once and replaced before Home Assistant rotates it."""
        now = self.clock()
        if not force and self._token and now - self._token_at < TOKEN_REFRESH:
            return self._token
        token = await self.token_of()
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError('Home Assistant gave no usable map token.')
        self._token, self._token_at = token, now
        return token

    def _quiet(self, error):
        """Leave Home Assistant alone for a while, and say so once."""
        self._quiet_until = self.clock() + UNAVAILABLE_COOLDOWN
        self._token = None
        if not self._said:
            LOG.info('Home Assistant serves no map tiles (%s): map cards are drawn without streets, and this app '
                     'asks again in %d minutes', type(error).__name__, UNAVAILABLE_COOLDOWN // 60)
            self._said = True

    # ----- the cache -----

    def _cached(self, key):
        item = self._cache.get(key)
        if item is None:
            return None
        raw, fetched_at = item
        if self.clock() - fetched_at > TILE_TTL:
            self._drop(key)
            return None
        self._cache.move_to_end(key)
        return raw

    def _drop(self, key):
        raw, _ = self._cache.pop(key)
        self._bytes -= len(raw)

    def _store(self, key, raw):
        if key in self._cache:
            self._drop(key)
        self._cache[key] = (raw, self.clock())
        self._bytes += len(raw)
        # Least recently used out first, until the cache is inside its bound again.
        while self._bytes > CACHE_BYTES and self._cache:
            self._drop(next(iter(self._cache)))

    # ----- the tiles -----

    async def _one(self, key, token, found, refused):
        if key in found:
            return
        try:
            raw = await self.fetch_tile(key[0], key[1], key[2], token)
        except Unauthorized:
            refused.append(key)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # One tile that will not come leaves its place empty; the card is still drawn.
            LOG.debug('No basemap tile %s (%s)', key, type(error).__name__)
            return
        if raw:
            self._store(key, raw)
            found[key] = raw

    async def _round(self, keys, token, found):
        """One pass over `keys`, at most MAX_CONCURRENT at a time; the keys whose token was refused."""
        gate = asyncio.Semaphore(MAX_CONCURRENT)
        refused = []

        async def guarded(key):
            async with gate:
                await self._one(key, token, found, refused)

        await asyncio.gather(*(guarded(key) for key in keys))
        return refused

    async def _gather(self, keys):
        """({key: raw}, whether the render may go on): the cache first, then one round and at most one retry."""
        found, wanted = {}, []
        for key in keys:
            raw = self._cached(key)
            if raw is not None:
                found[key] = raw
            elif key not in wanted:
                wanted.append(key)
        if not wanted:
            return found, True
        if not self.available():
            return found, bool(found)
        try:
            token = await self.token()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._quiet(error)
            return found, bool(found)
        refused = await self._round(wanted, token, found)
        if not refused:
            self._well()
            return found, True
        # A refused token is worth one new token and one more try; after that this render goes without streets.
        try:
            token = await self.token(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._quiet(error)
            return found, False
        if await self._round(refused, token, found):
            LOG.info('The map tile proxy refused this app twice: the map card is drawn without streets this time')
            return found, False
        self._well()
        return found, True

    def _well(self):
        self._said = False

    async def fetch(self, z, x, y):
        """One basemap tile, from the cache or through Home Assistant; None when it cannot be had right now."""
        found, _ = await self._gather([(int(z), int(x), int(y))])
        return found.get((int(z), int(x), int(y)))

    async def image(self, view, size):
        """The basemap under a frame, stitched and cropped to `size`, or None when the card goes without streets.

        RGBA: where a tile is missing the picture is see-through, so the render's own background shows there. How
        many tiles were missing is in `info['map_missing']`: a picture with a hole in it is provisional, and the
        caller keeps it out of its own caches so the next render tries those tiles again (map_card.render).
        """
        keys = view.tile_keys()
        if not keys or len(keys) > MAX_TILES or not all(_bounded(key) for key in keys):
            # The caller reduces the zoom until the basemap fits (map_card.viewport); this refuses to flood.
            LOG.debug('A map frame asked for %d basemap tiles, over the budget of %d', len(keys), MAX_TILES)
            return None
        found, ok = await self._gather(keys)
        if not ok or not found:
            return None
        return self._stitch(view, size, found)

    def _stitch(self, view, size, found):
        from PIL import Image
        mosaic, crop = view.mosaic()
        canvas = Image.new('RGBA', (int(mosaic[0]), int(mosaic[1])), (0, 0, 0, 0))
        drawn, missing = 0, 0
        for key, (left, top) in view.tile_places():
            raw = found.get(key)
            if raw is None:
                missing += 1
                continue
            try:
                with Image.open(io.BytesIO(raw)) as tile:
                    tile.draft('RGB', (TILE_PX, TILE_PX))
                    square = tile.convert('RGB')
                    if square.size != (TILE_PX, TILE_PX):
                        square = square.resize((TILE_PX, TILE_PX), Image.Resampling.BILINEAR)
            except Exception as error:
                # Whatever this is, it is no tile: it is dropped rather than kept for a week, so a proxy that
                # answered one page of HTML does not leave the card without streets until the cache expires.
                LOG.debug('A basemap tile %s cannot be read (%s)', key, type(error).__name__)
                self._forget(key)
                missing += 1
                continue
            canvas.paste(square, (int(left), int(top)))
            drawn += 1
        if not drawn:
            return None
        image = canvas.resize((max(1, int(size[0])), max(1, int(size[1]))), Image.Resampling.BICUBIC, box=crop)
        image.info['map_missing'] = missing
        return image

    def _forget(self, key):
        """Drop a tile from the cache; harmless when it is not in it."""
        if key in self._cache:
            self._drop(key)


def _bounded(key):
    """A tile the proxy could serve: a zoom it knows and a place inside the map at that zoom."""
    zoom, x, y = key
    span = 1 << zoom
    return 0 <= zoom <= RASTER_MAX_ZOOM and 0 <= x < span and 0 <= y < span
