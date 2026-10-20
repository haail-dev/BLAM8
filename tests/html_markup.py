from html.parser import HTMLParser


class Element:
    def __init__(self, tag, attrs, start=0):
        self.tag, self.attrs, self.start = tag, dict(attrs), start
        self.children = []
        self.text = ''

    def find_all(self, tag=None, **attrs):
        return [child for node in self.children
                for child in [node, *node.find_all()]
                if (tag is None or child.tag == tag)
                and all(key in child.attrs and (value is None or child.attrs[key] == value)
                        for key, value in attrs.items())]

    def one(self, tag=None, **attrs):
        matches = self.find_all(tag, **attrs)
        assert len(matches) == 1, (tag, attrs, len(matches))
        return matches[0]


class _Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.root = Element('', [])
        self.stack = [self.root]
        self.lines = source.splitlines(keepends=True)
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        line, column = self.getpos()
        node = Element(tag, attrs, sum(map(len, self.lines[:line - 1])) + column)
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                       'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node.text += data


def markup(source):
    """Inspect actual tags; CSS selectors and script strings are text, not elements.

    Unrendered Jinja control flow is retained as text; render templates when
    checking which conditional branch is visible.
    """
    return _Markup(source).root
