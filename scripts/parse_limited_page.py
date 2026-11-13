"""艦これ攻略Wiki「期間限定ドロップイベント」をブラウザで保存した HTML から、
イベントごとの限定ドロップを読み取り、data/limited_page.json に書き込む。

使い方:  pip install beautifulsoup4 lxml
         python scripts/parse_limited_page.py 期間限定ドロップイベント.html [アーカイブ_終了済ドロップ.html ...]
  (折りたたみは開いた状態で保存したもの。アーカイブのイベントはすべて「終了」として扱う)

data/limited_page.json に足す内容:
  events  : [{"title", "start", "end", "status"}] イベント名・開始日・終了日・継続中/一部継続中/終了
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

events, entries, ongoing = [], [], {}
DATE = re.compile(r'(\d{4})/(\d{1,2})/(\d{1,2})')


def ymd(m):
    return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"


def parse_file(path):
    h = open(path, encoding='utf-8', errors='replace').read()
    s = BeautifulSoup(h, 'lxml')
    title_tag = s.title.get_text() if s.title else ''
    archive = 'アーカイブ' in title_tag or '終了済' in title_tag
    n_ev = 0
    HEAD = re.compile(r'^(.*?)\s*[\(（]\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*[～~〜]\s*([^)）]*)[\)）]?')
    for h2 in s.find_all('h2'):
        title = h2.get_text(' ', strip=True)
        m = HEAD.match(title)
        if not m: continue
        name = re.sub(r'\s+', ' ', m.group(1)).strip()
        name = re.sub(r'\s*([【】、（）()])\s*', r'\1', name)   # リンクの前後に入る空白を詰める
        start = f"{m.group(2)}/{int(m.group(3)):02d}/{int(m.group(4)):02d}"
        ei = len(events)
        rest = m.group(5).strip()
        dm = DATE.search(rest)
        end = ymd(dm) if dm else None
        status = '終了' if (archive or (end and '継続' not in rest)) else rest
        events.append({"title": name, "start": start, "end": end, "status": status})
        n_ev += 1
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
                        entries.append([mp, node, n, ei, int(ended or status == '終了')])

    # 先頭の「現在継続中」一覧 (海域単位)
    found = {}
    for tb in s.find_all('table'):
        p = tb.find_previous(['h2', 'h3'])
        if not p or '現在継続中' not in p.get_text(): continue
        for r in grid(tb)[1:]:
            if len(r) < 3: continue
            ia = 1 if len(r) == 3 else 0
            for mp, _ in areas(r[ia].get_text(' ', strip=True)):
                for n, ended in ships_in(r[-1]):
                    if not ended: found.setdefault(mp, []).append(n)
        if found:
            break
    if found and not archive:
        ongoing.clear()
        ongoing.update(found)
    print(f"{os.path.basename(path)}: {'アーカイブ' if archive else '期間限定ドロップイベント'} / イベント {n_ev} 件")


for path in sys.argv[1:]:
    parse_file(path)

entries = [list(x) for x in dict.fromkeys(tuple(e) for e in entries)]
if not events:
    sys.exit("表を読み取れませんでした。期間限定ドロップのページを保存した HTML か確認してください")
path = os.path.join(ROOT, 'data', 'limited_page.json')
page = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
page.update({"source": "艦これ攻略Wiki 出撃ドロップ / 期間限定ドロップイベント / 終了済アーカイブ",
             "events": events, "entries": entries})
if ongoing:
    page["ongoing"] = {k: sorted(set(v)) for k, v in ongoing.items()}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(page, f, ensure_ascii=False, separators=(',', ':'))
    f.write('\n')
print('合計: イベント', len(events), '/ 艦', len(entries), '（うち終了', sum(e[4] for e in entries), '）/ 継続中一覧', len(ongoing or page.get('ongoing', {})), '海域')
