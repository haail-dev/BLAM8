"""Generate exact-contour Mist samples from the shipped BLAM8 motion mark.

The body, two gear cutouts, two eyes and three speed strokes all come from
blam8-mark-motion.js, itself generated from the source logo SVG.
"""
import argparse
import base64
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'src/app/static/blam8-mark-motion.js'
OUTPUT = ROOT / 'src/app/static/blam8-particle-map.js'
SVG = '{http://www.w3.org/2000/svg}'


def stable(value):
    """Round away libm last-digit differences (atan2, pow) between platforms.

    The committed map was made on Windows and must regenerate byte for byte on
    Linux CI. Ordering and floor() decisions made on values rounded to nine
    decimals do not depend on which C library produced the last digit. Used for
    the body fill only: the rank column (material identity) is pinned by
    tests/test_glass_core.py and already matches on both platforms."""
    return round(value, 9)


def polygon(path):
    return [(float(x), float(y)) for x, y in re.findall(r'[ML]\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)', path.attrib['d'])]


def inside(vertices, x, y):
    hit = False
    for i, a in enumerate(vertices):
        b = vertices[i-1]
        if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            hit = not hit
    return hit


def clearance(contours, x, y):
    """Distance to the nearest genuine contour edge, in logo view-box units."""
    nearest = float('inf')
    for vertices in contours:
        for index, a in enumerate(vertices):
            b = vertices[index - 1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            segment = dx*dx + dy*dy
            along = max(0.0, min(1.0, ((x-a[0])*dx+(y-a[1])*dy)/segment)) if segment else 0.0
            ex, ey = x-a[0]-along*dx, y-a[1]-along*dy
            nearest = min(nearest, ex*ex+ey*ey)   # multiply, not **2: no libm pow
    return math.sqrt(nearest)


def generate():
    source = SOURCE.read_text(encoding='utf-8')
    match = re.search(r'export const BLAM8_MOTION_MARK = (".*?");', source, re.S)
    if not match:
        raise RuntimeError('Generated source mark missing')
    root = ET.fromstring(json.loads(match.group(1)))
    children = [item for item in root if item.tag == SVG + 'path']
    body, top_eye, lower_eye, *strokes = map(polygon, children)
    gears = [polygon(item) for item in root.iter(SVG + 'path') if 'vl-gear' in item.attrib.get('class', '')]
    if len(gears) != 2 or len(strokes) != 3:
        raise RuntimeError('Expected eight source contours')
    state = 5317

    def random():
        nonlocal state
        state = (state*1664525+1013904223) & 0xffffffff
        return state/4294967296

    def occupies(x, y):
        return ((inside(body, x, y) and not inside(gears[0], x, y) and not inside(gears[1], x, y))
                or inside(top_eye, x, y) or inside(lower_eye, x, y)
                or any(inside(stroke, x, y) for stroke in strokes))

    contours = [body, *gears, top_eye, lower_eye, *strokes]
    samples = []
    while len(samples) < 3000:
        x, y = 18+64*random(), 17+66*random()
        if not occupies(x, y):
            continue
        bx, by = round((x-18)/64*255), round((y-17)/66*255)
        # Quantization must preserve the small gear holes and speed strokes.
        if not occupies(18+bx*64/255, 17+by*66/255):
            continue
        # 85% of the nearest edge distance keeps the complete orbit within
        # the real fill, including the gear's negative space and narrow strokes.
        radius = min(5.5, clearance(contours, 18+bx*64/255, 17+by*66/255)*.85)
        samples.append((bx, by, math.floor(radius/5.5*255)))
    # Preserve neighbourhoods as the B unfolds. Arbitrary particle-to-ribbon
    # assignments collapse through the centre into a bright blob mid-transition.
    order = sorted(range(len(samples)), key=lambda i: math.atan2(
        50-(17+samples[i][1]*66/255), (18+samples[i][0]*64/255)-50))
    ranks = [0] * len(samples)
    for rank, index in enumerate(order):
        ranks[index] = round(rank / (len(samples)-1) * 255)
    # Fill the body behind its mechanical cutouts. The GPU applies the exact
    # independently rotating gear masks; static negative-space samples would
    # otherwise conceal that motion. Preserve every old material phase byte.
    body_samples=[]
    def body_occupies(x,y):
        return inside(body,x,y) or any(inside(stroke,x,y) for stroke in strokes)
    while len(body_samples)<3000:
        x,y=18+64*random(),17+66*random()
        bx,by=round((x-18)/64*255),round((y-17)/66*255)
        x,y=18+bx*64/255,17+by*66/255
        if not body_occupies(x,y):continue
        radius=min(5.5,clearance([body,*strokes],x,y)*.85)
        body_samples.append((bx,by,math.floor(stable(radius/5.5*255))))
    body_order=sorted(range(3000),key=lambda i:stable(math.atan2(50-(17+body_samples[i][1]*66/255),(18+body_samples[i][0]*64/255)-50)))
    paired=[None]*3000
    for rank,index in enumerate(order):paired[index]=body_samples[body_order[rank]]
    data=bytearray()
    for index,sample in enumerate(paired):data.extend((*sample,ranks[index]))
    encoded=base64.b64encode(data).decode('ascii')
    # Four original contours, packed at four bits/texel: two gears and hubs.
    # Local 32-unit squares oversample each contour without runtime SVG sampling.
    centers=[(63.45,33.8),(56.8,64.4)]
    bits=bytearray(128*128//2)
    for y in range(128):
        for x in range(128):
            value=0
            for channel,vertices in enumerate([*gears,top_eye,lower_eye]):
                cx,cy=centers[channel%2]
                if inside(vertices,cx+((x+.5)/128-.5)*32,cy-((y+.5)/128-.5)*32):value|=1<<channel
            index=y*128+x;bits[index//2]|=value<<((index%2)*4)
    mask=base64.b64encode(bits).decode('ascii')
    return ('// Generated by scripts/build/derive_blam8_particle_map.py from the exact motion-mark contours.\n'
            +f'export const BLAM8_PARTICLE_MAP = "{encoded}";\n'
            +f'export const BLAM8_GEAR_MASK = "{mask}";\n')



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = generate()
    if args.check:
        if OUTPUT.read_text(encoding='utf-8') != expected:
            raise SystemExit('Particle map does not match source mark')
        print('Particle map/material phases and mechanical masks match original contours')
    else:
        OUTPUT.write_text(expected, encoding='utf-8')
