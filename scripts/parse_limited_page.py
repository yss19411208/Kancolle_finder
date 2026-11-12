"""艦これ攻略Wiki「期間限定ドロップイベント」をブラウザで保存した HTML から、
イベントごとの限定ドロップを読み取り、data/limited_page.json に書き込む。

使い方:  pip install beautifulsoup4 lxml
         python scripts/parse_limited_page.py 保存したページ.html

data/limited_page.json に足す内容:
  events  : [{"title", "start", "status"}]        イベント名・開始日・継続中/一部継続中
  entries : [[海域, マス|null, 艦名, イベント番号, 終了(0/1)]]   取消線の艦は 終了=1
  ongoing : {海域: [艦名]}                          ページ先頭の「現在継続中」一覧
(出撃ドロップの青字 "blue" はそのまま残す)
"""
import sys, re, json, os
from bs4 import BeautifulSoup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from update import norm_name

ships = json.load(open(os.path.join(ROOT, 'data', 'ship_master.json'), encoding='utf-8'))
names = {norm_name(v['name']): v['name'] for v in ships.values()}
if len(sys.argv) < 2:
    sys.exit(__doc__)
h = open(sys.argv[1], encoding='utf-8', errors='replace').read()
s = BeautifulSoup(h, 'lxml')

def grid(tb):
    """rowspan / colspan を展開した表 (td/th の要素の 2 次元配列)"""
    out, carry = [], {}
    for tr in tb.find_all('tr'):
        row, ci = [], 0
        cells = tr.find_all(['td', 'th'], recursive=False)
        it = iter(cells)
        while True:
            if ci in carry:
                el, left = carry[ci]
                row.append(el)
                if left > 1: carry[ci] = (el, left - 1)
                else: del carry[ci]
                ci += 1; continue
            c = next(it, None)
            if c is None:
                if any(k >= ci for k in carry): ci += 1; continue
                break
            rs, cs = int(c.get('rowspan', 1) or 1), int(c.get('colspan', 1) or 1)
            for _ in range(cs):
                row.append(c)
                if rs > 1: carry[ci] = (c, rs - 1)
                ci += 1
        out.append(row)
    return out

AREA = re.compile(r'(\d-\d)(?:-((?:[A-Z]\d?)(?:\s*[,、]\s*[A-Z]\d?(?![-\d]))*))?')
def areas(text):
    res = []
    for m in AREA.finditer(text):
        if m.group(2):
            for n in re.split(r'\s*[,、]\s*', m.group(2)): res.append((m.group(1), n))
        else:
            res.append((m.group(1), None))   # 「1-1(全域)」など、マス指定なし
    return res

def ships_in(td):
    res = []
    for a in td.find_all('a'):
        t = a.get_text(' ', strip=True)
        n = names.get(norm_name(t))
        if not n: continue
        ended = a.find_parent(['del', 's', 'strike']) is not None
        res.append((n, ended))
    return res

events, entries, bad = [], [], set()
HEAD = re.compile(r'^(.*?)\s*[\(（]\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*[～~〜]\s*([^)）]*)[\)）]?')
for h2 in s.find_all('h2'):
    title = h2.get_text(' ', strip=True)
    m = HEAD.match(title)
    if not m: continue
    name = re.sub(r'\s+', ' ', m.group(1)).replace(' 、', '、').strip()
    start = f"{m.group(2)}/{int(m.group(3)):02d}/{int(m.group(4)):02d}"
    ei = len(events)
    events.append({"title": name, "start": start, "status": m.group(5).strip()})
    # 次の h2 までの表
    for el in h2.find_all_next(['h2', 'table']):
        if el.name == 'h2': break
        rows = grid(el)
        if not rows: continue
        hdr = [c.get_text(strip=True) for c in rows[0]]
        if 'ドロップ対象' not in hdr or 'エリア' not in hdr: continue
        ia, iship = hdr.index('エリア'), hdr.index('ドロップ対象')
        for r in rows[1:]:
            if len(r) <= max(ia, iship): continue
            ar = areas(r[ia].get_text(' ', strip=True))
            sh = ships_in(r[iship])
            if not ar or not sh: continue
            for mp, node in ar:
                for n, ended in sh:
                    entries.append([mp, node, n, ei, int(ended)])

# 先頭の「現在継続中」一覧 (海域単位)
ongoing = {}
for tb in s.find_all('table'):
    p = tb.find_previous(['h2', 'h3'])
    if not p or '現在継続中' not in p.get_text(): continue
    for r in grid(tb)[1:]:
        if len(r) < 3: continue
        ia = 1 if len(r) == 3 else 0
        for mp, _ in areas(r[ia].get_text(' ', strip=True)):
            for n, ended in ships_in(r[-1]):
                if not ended: ongoing.setdefault(mp, []).append(n)
    if ongoing:
        break
entries = [list(x) for x in dict.fromkeys(tuple(e) for e in entries)]
if not events or not ongoing:
    sys.exit("表を読み取れませんでした。「期間限定ドロップイベント」のページを保存した HTML か確認してください")
path = os.path.join(ROOT, 'data', 'limited_page.json')
page = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
page.update({"source": "艦これ攻略Wiki 出撃ドロップ / 期間限定ドロップイベント",
             "events": events, "entries": entries,
             "ongoing": {k: sorted(set(v)) for k, v in ongoing.items()}})
with open(path, 'w', encoding='utf-8') as f:
    json.dump(page, f, ensure_ascii=False, separators=(',', ':'))
    f.write('\n')
print('events', len(events), 'entries', len(entries), 'ended', sum(e[4] for e in entries), 'ongoing maps', len(ongoing))
