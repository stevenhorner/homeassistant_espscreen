# A map card

App 0.4.24 with firmware 0.15.0 puts a map on a person tile: the basemap Home Assistant serves,
the zones around it, and a marker for everyone the card follows. The add-on draws the whole card
and sends the screen a picture, the same way a live camera arrives, so the screen holds no
location, no token and no map address.

## What it shows

- **One person.** Drag a `person.*` entity onto a cell and set **Display** to **Map**. That is the
  whole configuration: the card shows the streets around that person, their marker, the zones
  nearby, the tile's name in the band at the bottom, and the attribution the basemap comes with.
- **Several people.** The inspector's **On the map** row lists who else rides along. The tile's own
  entity is always first and cannot be removed; **Add entity** offers people and device trackers.
  At most eight in all, so a phone or a car joins the card without needing a person entity.
- **Zones.** Every zone Home Assistant has a place for is drawn as a circle with its name. A zone
  only widens the view when someone on the card is standing in it, so a holiday zone on another
  continent cannot flatten the map.
- **A legend.** From a 1x2 card up, the people are listed under the map with their state, at most
  four rows and then "+2 more". A full-page card lists them all, in two columns when it is much
  wider than tall.
- **Who is not on the map.** Somebody with no coordinates at all is counted in a line under the
  legend rather than being drawn in the wrong place; somebody whose state names a zone gets a
  hollow marker in the middle of that zone, to say the place is the zone's and not theirs. With
  nobody anywhere the card says "No location" and keeps its name.

## Which screens

Every board that draws pictures: the 4-inch, 7-inch and 10.1-inch Guition, the JC3248W535, and the
4B, 4.3-inch and 7-inch Waveshare. Which boards those are is `camera` in
`screen_manager/app/boards.json`, worked out from whether the board file includes
`packages/features/camera.yaml`. The CYD, the Waveshare 3.5-inch and the Hosyond 4-inch have no
memory for pictures; the editor does not offer Map there and a save with one is refused with "This
screen cannot show a map". A screen whose firmware is older than 0.15.0 is told to update first.

## The two base maps

**Map from Home Assistant** (the default). Home Assistant Core carries a `map_tiles` integration:
it hands out a short-lived token over the websocket and serves `/api/map_tiles/raster/{z}/{x}/{y}.png`,
fetching those tiles from OpenStreetMap with its own identification and keeping them for seven days.
The add-on rides on exactly that. It never contacts a tile server itself, and a tile request is a
zoom and two whole numbers: no name, no entity, nothing about who is on the card. The add-on keeps
its own copy of the tiles as well, so a page that reloads for its camera does not fetch the same
streets again.

**None.** Nothing leaves Home Assistant. The card draws your zones and the people on them on a plain
themed background. This is the choice for a household that does not want map requests made on its
behalf, and it is also what a card falls back to when the house has no internet, when Home Assistant
has no `map_tiles` integration, or when the proxy refuses. That fallback is silent on the glass: the
card simply has no streets, which is what **None** looks like anyway. The log says so once and the
add-on then leaves Home Assistant alone for ten minutes before trying again.

A card too short to carry a legible attribution (under about 60 pixels of height) does not use the
basemap at all and is drawn plain instead. The licence is a condition of using the tiles, so it is
never traded away for a thumbnail.

## When it is drawn again

Never on a clock. The add-on puts a short mark in the tile's state, worked out from the stored
choices of the card, the quantised places and states of the people it shows, and a digest of the
zones. The screen folds that mark into the picture it asks for, so a changed mark is a changed
picture and an unchanged mark is no request at all. Standing still for an hour costs nothing: no
download, no render, no tile request. Places are rounded to a grid of about 25 metres, so a phone
reporting a few metres of drift does not make the screen fetch a new picture.

The mark is deliberately independent of the frame, the look and the base map: the size of the card
and light or dark are already part of what the screen asks for, so putting them in the mark as well
would only cost pictures.

Turning to another page and back shows the last map at once, because the picture is kept under the
same name the camera pictures are. Nothing loads in standby, under a finger, under an open card, or
between two quick page turns.

## Privacy and the network

- **No coordinate reaches a screen.** The screen gets pixels. The companions, the zoom, the labels
  and the base map are stored in the add-on and stripped before the layout goes out.
- **The picture travels unencrypted over the LAN**, on port 8098, like every other picture. A map
  card shows roughly where someone is to anyone on that network. That is worth knowing if a screen
  sits on a guest VLAN.
- **A link is random and short lived**, and is only issued for a map tile that really is on that
  screen's saved layout. A screen cannot name an entity it has no map tile for, and cannot change
  which people a card draws: the add-on renders from the saved tile, never from the request.
- **With the base map set to None nothing leaves Home Assistant at all.**

## Memory and speed

A map frame is one frame of the page's existing picture, so there is no second buffer on the screen
and the shared caps hold: at most 1024 pixels either way and 1.25 MB once decoded. One card needs at
most twelve basemap tiles, fetched four at a time, and only when the view has moved off the tiles
already in hand; a household that stays in one town fetches its tiles once and then not again for a
week. A busy card (eight people, twelve zones, 480x320) draws in well under 50 ms.

A basemap that came back with a hole in it is drawn but not kept: the next load asks for those tiles
again, so the streets come back by themselves. A tile the proxy answered with something that is not a
picture is dropped instead of being held for a week.

## Version 1

The tile's own entity is a person. A phone or a car without a person entity is added as a companion,
not as the tile itself. Tapping the card opens the ordinary detail card, which for a person already
carries a history timeline. A full-screen map and history trails are for a later release; neither is
blocked by how this is built.

## For developers

| Piece | Where |
| --- | --- |
| Geometry, the movement mark and the drawing | `screen_manager/app/map_card.py` |
| The basemap client, through Home Assistant's proxy | `screen_manager/app/map_tiles.py` |
| The two Home Assistant calls | `HomeAssistant.map_tiles_token` and `map_tile` in `server.py` |
| Authorising and rendering a screen's request | `Manager.answer_live` and `map_render` in `server.py` |
| Putting a drawn frame in the page's picture | `tile_art.encode`, `camera_feed.live` with `renders` |
| What the firmware does with it | `Tile::is_map()`, `pictured()`, `card_art`, `live_wanted` |
| The editor | `TileInspector.vue`, `TileCard.vue`, `model/tile-options.ts` |
| Tests | `tests/test_map_card.py`, `test_map_tiles.py`, `test_map_delivery.py` |

No map arithmetic exists in C++: the add-on draws the whole frame and `render_camera_card` places
it, unchanged. See `docs/CAMERA.md` for the picture pipeline the map shares.
