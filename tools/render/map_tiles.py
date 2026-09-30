"""Render the map card at every size on every board, with a stubbed basemap so nothing touches the network.

    python3 tools/render/map_tiles.py --cards                 the add-on's own cards, no ESPHome needed
    python3 tools/render/map_tiles.py guition waveshare43     the same cards on the real firmware (needs ESPHome and SDL2)

The add-on draws the whole map card (screen_manager/app/map_card.py) and the firmware only places the bitmap, so
`--cards` is the honest way to judge the card itself: the fit, the zone circles, the markers and their labels, the
legend, the scale bar and the attribution, in both looks, at the frame each board's cells really give it. The basemap
is drawn here instead of fetched, in the greens and greys of the real one, so this script never asks Home Assistant
or a tile server for anything (docs/MAP.md).

With board keys it compiles the firmware as a host program (tools/render/host.py) and lets it ask for its pictures the
way it asks ESP Screens, so the placement, the corner radius and the name band can be checked as well.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Only the card path is imported here: tools/render/run.py needs aioesphomeapi, which a machine that just wants to
# look at the cards has no reason to have.
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / 'screen_manager/app'))
import map_card  # noqa: E402

# The frames a card really gets, from the smallest glass that draws pictures to the largest full page.
FRAMES = {'single': (150, 118), 'wide': (312, 118), 'tall': (150, 244), 'square': (312, 244), 'full': (464, 396),
          'page-10inch': (1024, 640)}
# Made-up coordinates in the middle of the North Sea: no address of anyone's is in this repository.
HOME = 54.0, 3.0


def demo():
    """(the tile, the states) of a busy card: four people, a device tracker and two zones."""
    states = {
        'zone.home': {'state': 'zoning', 'attributes': {'friendly_name': 'Home', 'latitude': HOME[0], 'longitude': HOME[1], 'radius': 120}},
        'zone.work': {'state': 'zoning', 'attributes': {'friendly_name': 'Work', 'latitude': HOME[0] + 0.012, 'longitude': HOME[1] + 0.021, 'radius': 200}},
        'person.max': {'state': 'home', 'attributes': {'friendly_name': 'Max', 'latitude': HOME[0] + 0.0008, 'longitude': HOME[1] + 0.0011}},
        'person.robin': {'state': 'Work', 'attributes': {'friendly_name': 'Robin', 'latitude': HOME[0] + 0.0122, 'longitude': HOME[1] + 0.0205}},
        'person.sam': {'state': 'not_home', 'attributes': {'friendly_name': 'Sam', 'latitude': HOME[0] + 0.0061, 'longitude': HOME[1] + 0.0118}},
        'person.guest': {'state': 'unavailable', 'attributes': {'friendly_name': 'Guest'}},
        'device_tracker.phone': {'state': 'home', 'attributes': {'friendly_name': 'Phone', 'latitude': HOME[0] + 0.0003, 'longitude': HOME[1] + 0.0019}},
        'device_tracker.car': {'state': 'not_home', 'attributes': {'friendly_name': 'Car', 'latitude': HOME[0] + 0.0089, 'longitude': HOME[1] + 0.0152}},
    }
    tile = {'entity': 'person.max', 'name': 'Max', 'options': {
        'display': 'map', 'map': ['person.robin', 'person.sam', 'person.guest', 'device_tracker.phone', 'device_tracker.car']}}
    return tile, states


def basemap(view, size, dark=False):
    """A stand-in for map_tiles.TileSource.image: streets drawn here, so nothing is fetched from anywhere.

    Whole world coordinates, so the pattern moves with the viewport exactly as a real mosaic would."""
    from PIL import Image, ImageDraw
    width, height = int(size[0]), int(size[1])
    image = Image.new('RGBA', (width, height), (233, 231, 226, 255))
    draw = ImageDraw.Draw(image)
    left, top = view.source_origin()
    step = 34
    # Blocks of green and grey between the streets, placed from the basemap's own origin.
    for gy in range(-1, height // step + 2):
        for gx in range(-1, width // step + 2):
            x0, y0 = gx * step - int(left) % step, gy * step - int(top) % step
            shade = (214, 228, 205, 255) if (gx + gy) % 3 == 0 else (238, 236, 231, 255)
            draw.rectangle((x0 + 3, y0 + 3, x0 + step - 4, y0 + step - 4), fill=shade)
    for gx in range(-1, width // step + 2):
        x0 = gx * step - int(left) % step
        draw.line((x0, 0, x0, height), fill=(255, 255, 255, 255), width=3)
    for gy in range(-1, height // step + 2):
        y0 = gy * step - int(top) % step
        draw.line((0, y0, width, y0), fill=(255, 255, 255, 255), width=3)
    image.info['map_missing'] = 0
    return image


def cards(out):
    """One sheet per frame size, light and dark, plus a card with no basemap and one with nobody anywhere."""
    from PIL import Image, ImageDraw
    out.mkdir(parents=True, exist_ok=True)
    tile, states = demo()
    made = 0
    for name, size in FRAMES.items():
        shots = []
        for label, card, dark, streets in (('light', tile, False, True), ('dark', tile, True, True),
                                           ('no base map', {**tile, 'options': {**tile['options'], 'basemap': 'none'}}, False, False),
                                           ('no location', {'entity': 'person.guest', 'name': 'Guest', 'options': {'display': 'map'}}, False, True)):
            view = map_card.view_of(size, card, states)
            picture = basemap(view, size) if streets and map_card.wants_basemap(card, size) else None
            shots.append((label, map_card.render(size, card, states, dark=dark, basemap_image=picture)))
            made += 1
        gap, head = 16, 26
        sheet = Image.new('RGB', (len(shots) * (size[0] + gap) + gap, size[1] + head + 2 * gap), (245, 246, 248))
        draw = ImageDraw.Draw(sheet)
        for index, (label, shot) in enumerate(shots):
            x = gap + index * (size[0] + gap)
            draw.text((x, gap), f'{label}  {size[0]}x{size[1]}', fill=(30, 40, 50), font=map_card._font(13, bold=True))
            sheet.paste(shot, (x, gap + head))
        sheet.save(out / f'map-{name}.png')
    print(f'{made} cards in {len(FRAMES)} sheets under {out}')
    return made


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('variants', nargs='*', help='board keys for the firmware render; none with --cards')
    parser.add_argument('--cards', action='store_true', help="only the add-on's own cards, without ESPHome")
    parser.add_argument('--out', type=Path, default=REPO / '.esphome' / 'map-tiles' / 'out')
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.cards or not args.variants:
        cards(args.out)
        return 0
    import os
    import shlex
    import host
    import run
    esphome = shlex.split(os.environ.get('ESPHOME', 'esphome'))
    cards(args.out)
    for key in args.variants:
        item = host.variant(key)
        build = host.Build(item, tree=run.REPO, work=None, esphome=esphome)
        ok, output = build.compile()
        print(f'{key}: {"built" if ok else "BUILD FAILED"}', flush=True)
        if not ok:
            print(output[-2000:])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
