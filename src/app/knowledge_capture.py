"""Website evidence blocks. Location is context, never a reason to drop a rule."""
from hashlib import sha256
from html.parser import HTMLParser
import re


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.blocks = [], [], []

    def flush(self):
        text = ' '.join(''.join(self.parts).split())
        self.parts = []
        if text:
            location = next((t for t in reversed(self.stack) if t in {'header', 'footer', 'aside', 'nav', 'h1', 'h2', 'h3', 'h4'}), 'body')
            self.blocks.append({'text': text, 'location': location})

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.parts.append(' ')
        if tag in {'p', 'div', 'li', 'section', 'article', 'main', 'header', 'footer', 'aside', 'nav', 'br', 'h1', 'h2', 'h3', 'h4', 'tr'}:
            self.flush()
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'a':
            self.parts.append(' ')
        if tag in {'p', 'div', 'li', 'section', 'article', 'main', 'header', 'footer', 'aside', 'nav', 'h1', 'h2', 'h3', 'h4', 'tr'}:
            self.flush()
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        if not any(t in self.stack for t in {'head', 'script', 'style', 'noscript', 'svg', 'template'}):
            self.parts.append(data)


def extract(html):
    """Retain non-main text too, including mixed navigation/policy elements.

    Navigation remains labelled evidence for an explicit reviewer decision. No
    keyword heuristic is allowed to silently discard a potentially material rule.
    """
    parser = _Blocks()
    parser.feed(html)
    parser.flush()
    blocks, offset = [], 0
    for block in parser.blocks:
        blocks.append({**block, 'start': offset, 'end': offset + len(block['text'])})
        offset += len(block['text']) + 1
    return {'text': '\n'.join(b['text'] for b in blocks), 'blocks': blocks}


def bounded(capture, limit):
    """Cut at complete block/sentence boundaries; disclose every omitted char."""
    text, blocks = capture['text'], []
    for block in capture['blocks']:
        if block['end'] <= limit:
            blocks.append(block)
            continue
        # A very long paragraph can still retain complete sentences in order.
        available = text[block['start']:max(block['start'], limit)]
        ends = [m.end() for m in re.finditer(r'[.!?](?=\s|$)', available)]
        if ends:
            end = block['start'] + ends[-1]
            blocks.append({**block, 'text': text[block['start']:end], 'end': end})
        break
    end = blocks[-1]['end'] if blocks else 0
    return {'text': text[:end], 'blocks': blocks,
            'available_chars': len(text), 'retained_chars': end,
            'omitted_chars': len(text) - end,
            'available_hash': sha256(text.encode()).hexdigest()}
