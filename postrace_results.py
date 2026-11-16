"""Read public netkeiba result tables; payouts are yen per 100 yen.

This is a secondary source, not a JRA official API. Missing/ambiguous tables
raise rather than turning an unknown result into a losing ticket.
"""
import re
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

from keiba_scraper import JST, ids

KINDS = {'Tansho': '単勝', 'Fukusho': '複勝', 'Wakuren': '枠連',
         'Umaren': '馬連', 'Wide': 'ワイド', 'Umatan': '馬単',
         'Fuku3': '3連複', 'Tan3': '3連単'}
SIZES = {'単勝': 1, '複勝': 1, '枠連': 2, '馬連': 2,
         'ワイド': 2, '馬単': 2, '3連複': 3, '3連単': 3}


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)

    def find(self, tag=None, cls=None, **attrs):
        out = []
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if ((tag is None or child.tag == tag) and
                    (cls is None or cls in child.attrs.get('class', '').split()) and
                    all(child.attrs.get(k) == v for k, v in attrs.items())):
                out.append(child)
            out.extend(child.find(tag, cls, **attrs))
        return out


class Tree(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.stack[-1].children.append('\n')
            return
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {'img', 'meta', 'link', 'input', 'hr', 'area', 'source', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def selection(kind, value):
    ns = tuple(int(n) for n in str(value).split('-'))
    if kind not in SIZES or len(ns) != SIZES[kind] or any(n < 1 for n in ns):
        raise ValueError('invalid selection')
    if kind == '枠連':
        if any(n > 8 for n in ns):
            raise ValueError('invalid frame')
    elif len(set(ns)) != len(ns) or any(n > 18 for n in ns):
        raise ValueError('invalid horse selection')
    return '-'.join(map(str, sorted(ns) if kind not in ('馬単', '3連単') else ns))


def parse_result(page, race_id, fetched_at):
    root = Tree(page).root
    _, nk_id, _ = ids(race_id)
    canonical = root.find('link', rel='canonical')
    if not canonical or not re.search(r'race_id=' + re.escape(nk_id) + r'(?:&|$)',
                                      canonical[0].attrs.get('href', '')):
        raise ValueError('result page identity mismatch')
    tables = root.find('table', id='All_Result_Table')
    if len(tables) != 1:
        raise ValueError('result table unavailable')
    finish, scratched = {}, []
    for row in tables[0].find('tr', 'HorseList'):
        ranks = row.find(cls='Rank')
        numbers = row.find('td', 'Txt_C')
        numbers = [n for n in numbers if 'Num' in n.attrs.get('class', '').split()]
        if len(ranks) != 1 or len(numbers) != 1:
            raise ValueError('ambiguous result row')
        no, rank = int(numbers[0].text().strip()), ranks[0].text().strip()
        if no in finish:
            raise ValueError('duplicate finisher')
        if rank not in ('取消', '除外', '中止', '失格') and not re.fullmatch(r'\d+', rank):
            raise ValueError('unresolved finish rank')
        finish[no] = int(rank) if rank.isdigit() else rank
        if rank in ('取消', '除外'):
            scratched.append(no)
    if len(finish) < 3 or 1 not in finish.values():
        raise ValueError('incomplete race result')
    info = root.find(cls='RaceData02')
    count = re.search(r'(\d+)頭', info[0].text()) if info else None
    if count and len(finish) < int(count[1]):
        raise ValueError('result field is incomplete')
    payouts = {}
    for table in root.find('table', 'Payout_Detail_Table'):
        for row in table.find('tr'):
            kinds = [KINDS[c] for c in row.attrs.get('class', '').split() if c in KINDS]
            if not kinds:
                continue
            kind = kinds[0]
            result_cells, payout_cells = row.find('td', 'Result'), row.find('td', 'Payout')
            if len(result_cells) != 1 or len(payout_cells) != 1 or kind in payouts:
                raise ValueError('ambiguous payout table')
            cell = result_cells[0]
            groups = cell.find('ul')
            if groups:
                combos = [[li.text().strip() for li in g.find('li') if li.text().strip()]
                          for g in groups]
            else:
                # Win/place tables have several empty div placeholders per horse.
                combos = [[s.text().strip()] for s in cell.find('span') if s.text().strip()]
            amounts = re.findall(r'([\d,]+)\s*円', payout_cells[0].text())
            if len(combos) != len(amounts) or not combos:
                raise ValueError('payout/selection count mismatch')
            payouts[kind] = {}
            for combo, amount in zip(combos, amounts):
                key = selection(kind, '-'.join(combo))
                if key in payouts[kind]:
                    raise ValueError('duplicate payout')
                payouts[kind][key] = int(amount.replace(',', ''))
                if payouts[kind][key] <= 0:
                    raise ValueError('invalid payout')
    winners = {str(n) for n, rank in finish.items() if rank == 1}
    if set(payouts.get('単勝', {})) != winners:
        raise ValueError('winner and payout disagree')
    return {'race_id': race_id, 'finish': finish, 'scratched': scratched,
            'payouts': payouts, 'fetched_at': fetched_at.isoformat(),
            'source': 'netkeiba_public_result', 'official_verified': False,
            'source_url': f'https://race.netkeiba.com/race/result.html?race_id={nk_id}'}


def fetch_result(race_id):
    _, nk_id, _ = ids(race_id)
    url = f'https://race.netkeiba.com/race/result.html?race_id={nk_id}&rf=race_submenu'
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read()
        charset = response.headers.get_content_charset()
    if not charset:
        match = re.search(br'charset\s*=\s*["\']?([\w-]+)', body[:10000], re.I)
        charset = match.group(1).decode('ascii') if match else 'utf-8'
    return parse_result(body.decode(charset), race_id, datetime.now(JST))
