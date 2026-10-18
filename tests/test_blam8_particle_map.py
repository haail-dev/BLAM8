"""The Mist B is the actual signed-logo silhouette, including its cutouts."""
import base64
import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NS = '{http://www.w3.org/2000/svg}'


def test_particle_map_matches_genuine_contours():
    spec = importlib.util.spec_from_file_location('particle_generator', ROOT / 'scripts/build/derive_blam8_particle_map.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert (ROOT / 'src/app/static/blam8-particle-map.js').read_text(encoding='utf-8') == generator.generate()

    mark = (ROOT / 'src/app/static/blam8-mark-motion.js').read_text(encoding='utf-8')
    svg = ET.fromstring(json.loads(re.search(r'export const BLAM8_MOTION_MARK = (".*?");', mark, re.S).group(1)))
    children = [node for node in svg if node.tag == NS + 'path']
    body, top_eye, lower_eye, *strokes = map(generator.polygon, children)
    gears = [generator.polygon(node) for node in svg.iter(NS + 'path') if 'vl-gear' in node.attrib.get('class', '')]
    encoded = re.search(r'"([A-Za-z0-9+/=]+)"', (ROOT / 'src/app/static/blam8-particle-map.js').read_text()).group(1)
    points = base64.b64decode(encoded)
    assert len(points) == 12000
    # The cutouts are filled on purpose (20d3050b): the GPU applies the exact,
    # independently rotating gear masks, and samples held out of the holes would
    # hide that motion. So the invariant is "inside the body, an eye or a
    # stroke", with the gear holes counted rather than forbidden.
    counts = {'body': 0, 'top_eye': 0, 'lower_eye': 0, 'speed': 0, 'gear_hole': 0, 'outside': 0}
    ranks = points[3::4]
    assert min(ranks) == 0 and max(ranks) == 255
    for bx, by, br in zip(points[::4], points[1::4], points[2::4]):
        x, y = 18 + bx * 64/255, 17 + by * 66/255
        in_body = generator.inside(body, x, y)
        in_gear = any(generator.inside(gear, x, y) for gear in gears)
        eye_top = generator.inside(top_eye, x, y)
        eye_lower = generator.inside(lower_eye, x, y)
        stroke = any(generator.inside(s, x, y) for s in strokes)
        counts['body'] += in_body and not in_gear
        counts['top_eye'] += eye_top
        counts['lower_eye'] += eye_lower
        counts['speed'] += stroke
        counts['gear_hole'] += in_gear and not eye_top and not eye_lower
        counts['outside'] += not (in_body or eye_top or eye_lower or stroke)
        # Check the moving point at four phases of its bounded orbit, not just
        # its stationary seed. This includes the narrow speed strokes and eyes.
        radius = br/255*5.5*.94
        for ox, oy in ((radius, 0), (0, radius), (-radius, 0), (0, -radius)):
            assert (generator.inside(body, x+ox, y+oy)
                    or generator.inside(top_eye, x+ox, y+oy)
                    or generator.inside(lower_eye, x+ox, y+oy)
                    or any(generator.inside(stroke, x+ox, y+oy) for stroke in strokes)), (x, y, radius)
    assert counts['body'] + counts['gear_hole'] > 2300, counts
    assert counts['top_eye'] > 15 and counts['lower_eye'] > 15 and counts['speed'] > 100, counts
    assert counts['gear_hole'] > 300, counts   # the body is filled behind the cutouts
    assert counts['outside'] == 0, counts
