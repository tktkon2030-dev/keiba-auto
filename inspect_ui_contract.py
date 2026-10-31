"""Read public same-origin script text; never execute downloaded code."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen
import re

ROOT = 'https://euphoriaxx.ef4-db.workers.dev/'
class Scripts(HTMLParser):
    def __init__(self): super().__init__(); self.urls = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script' and a.get('src'): self.urls.append(urljoin(ROOT, a['src']))

def read(url):
    with urlopen(url, timeout=20) as r: return r.read(2_000_000).decode('utf-8')
p = Scripts(); p.feed(read(ROOT))
for url in p.urls[:8]:
    if urlsplit(url).netloc != urlsplit(ROOT).netloc: continue
    print('SCRIPT', url)
    text = read(url)
    for match in list(re.finditer(r'IM1|NW1|nw1|fetch\(', text))[:40]:
        print(text[max(0,match.start()-130):match.end()+200].replace('\n',' '))
