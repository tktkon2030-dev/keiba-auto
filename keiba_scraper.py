"""Existing two CSVs + Euphoria PL ticket probabilities; stdlib only."""
import argparse
import csv
import html as html_lib
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from euphoria_combinations import ticket_probabilities

JST = timezone(timedelta(hours=9))
SOURCE_ROOT = 'https://keiba-simu-euphoria.ef4-db.workers.dev'
VENUES = {1: '札幌', 2: '函館', 3: '福島', 4: '新潟', 5: '東京',
          6: '中山', 7: '中京', 8: '京都', 9: '阪神', 10: '小倉'}


def num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def rounded(value, digits=2, scale=1):
    v = num(value)
    return round(v * scale, digits) if v is not None else ''


def timestamp(value, date=None):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value = value.strip().replace('Z', '+00:00')
        if re.fullmatch(r'\d{2}:\d{2}(:\d{2})?', value):
            if not date:
                return None
            value = f'{date}T{value}'
        dt = datetime.fromisoformat(value)
        return (dt.replace(tzinfo=JST) if dt.tzinfo is None else dt).astimezone(JST)
    except ValueError:
        return None


def odds_clock(value, captured):
    # Only missing/unknown source time can be replaced; invalid/future time blocks EV.
    missing = value is None or str(value).strip() in ('', '不明', 'N/A', 'unknown')
    if missing:
        return captured, '保存時刻で代用', True
    dt = timestamp(value)
    if dt is None:
        return None, '原時刻の形式不正', False
    if dt > captured:
        return dt, '未来時刻のため無効', False
    return dt, 'API明示時刻', True


def ids(raw_id):
    raw_id = str(raw_id).replace('\\_', '_')
    parts = raw_id.split('_')
    if len(parts) == 6 and all(p.isdigit() for p in parts):
        return raw_id, f'{parts[0]}{parts[2]}{parts[3]}{parts[4]}{parts[5]}', int(parts[5])
    if re.fullmatch(r'\d{12}', raw_id):
        return raw_id, raw_id, int(raw_id[-2:])
    raise ValueError('unsupported race_id')


def fetch_json(date):
    url = f'{SOURCE_ROOT}/days/{date}.json'
    req = urllib.request.Request(url, headers={'User-Agent': 'KeibaCSV/3.0'})
    with urllib.request.urlopen(req, timeout=40) as res:
        return json.loads(res.read().decode('utf-8'))


def fetch_netkeiba_details(netkeiba_id):
    req = urllib.request.Request(
        f'https://race.netkeiba.com/race/shutuba.html?race_id={netkeiba_id}',
        headers={'User-Agent': 'Mozilla/5.0'})
    cookie = os.environ.get('NETKEIBA_COOKIE')
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            body = res.read()
            charset = res.headers.get_content_charset()
        if not charset:
            match = re.search(br'charset\s*=\s*["\']?([\w-]+)', body[:10000], re.I)
            charset = match.group(1).decode('ascii') if match else 'utf-8'
        page = body.decode(charset, errors='replace')
    except Exception as exc:
        # Never print URLs/cookies or webhook exceptions containing sensitive values.
        print(f'netkeiba補完失敗 [{netkeiba_id}]: {type(exc).__name__}')
        return '', {}
    name_m = re.search(r'<h1[^>]*class="[^"]*RaceName[^"]*"[^>]*>(.*?)</h1>', page, re.S)
    clean = lambda s: html_lib.unescape(re.sub(r'<[^>]+>', '', s)).strip()
    race_name = clean(name_m.group(1)) if name_m else ''
    horses = {}
    for row in re.findall(r'<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>(.*?)</tr>', page, re.S):
        u = re.search(r'<td[^>]*class="[^"]*Umaban[^"]*"[^>]*>\s*(\d+)\s*</td>', row)
        w = re.search(r'<td[^>]*class="[^"]*Waku[^"]*"[^>]*>.*?<span[^>]*>(\d+)</span>', row, re.S)
        j = re.search(r'<td[^>]*class="[^"]*Jockey[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', row, re.S)
        t = re.search(r'<td[^>]*class="[^"]*Trainer[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', row, re.S)
        if u:
            horses[int(u.group(1))] = {'枠': int(w.group(1)) if w else '',
                                      '騎手': clean(j.group(1)) if j else '',
                                      '調教師': clean(t.group(1)) if t else ''}
    return race_name, horses


def csv_string(rows):
    if not rows:
        return ''
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def build_files(raw_data, target_date, captured, card_fetcher=fetch_netkeiba_details):
    main_rows, history_rows, combo_rows, warnings = [], [], [], []
    races = raw_data.get('races')
    if not isinstance(races, list) or not races:
        raise ValueError('APIに有効なracesがありません')
    generated = timestamp(raw_data.get('generated_at'))
    late = raw_data.get('late') or {}
    late_races = late.get('races') or {}
    seen_races = set()
    for r in races:
        raw_id, nk_id, race_no = ids(r.get('race_id', ''))
        if raw_id in seen_races:
            raise ValueError(f'レースID重複: {raw_id}')
        seen_races.add(raw_id)
        if r.get('race_date') and r['race_date'] != target_date:
            raise ValueError(f'対象日不一致: {raw_id}')
        post = timestamp(r.get('post_time'), target_date)
        clock, clock_source, clock_ok = odds_clock(r.get('odds_snapshot_at'), captured)
        source_ok = generated is not None and generated <= captured
        valid = bool(post and captured < post and clock_ok and clock and source_ok)
        state = '未発走' if post and captured < post else '発走済み' if post else '発走時刻不明'
        if not source_ok:
            state = 'ソース生成時刻不明または未来'
        elapsed = round((captured - clock).total_seconds() / 60, 1) if clock else ''
        r_name, nk_horses = card_fetcher(nk_id)
        h_list = r.get('horses') or []
        if not isinstance(h_list, list) or not h_list:
            warnings.append(f'{raw_id}: 出走馬なし')
            continue
        no_list = [int(h['horse_no']) for h in h_list]
        if len(set(no_list)) != len(no_list):
            raise ValueError(f'馬番重複: {raw_id}')
        late_items = late_races.get(raw_id, []) if isinstance(late_races, dict) else []
        if not isinstance(late_items, list):
            late_items = []
        ld = {int(h['horse_no']): h for h in late_items if h.get('horse_no') is not None}
        # Top-level races is the canonical currently delivered snapshot. Late is
        # only a missing-field fallback; do not overwrite current values with locked data.
        merged = []
        for h in h_list:
            no = int(h['horse_no'])
            old = ld.get(no, {})
            mh = dict(h)
            fallback_fields = []
            for key, value in old.items():
                if mh.get(key) is None and value is not None:
                    mh[key] = value
                    fallback_fields.append(key)
            merged.append(mh)
            nkh = nk_horses.get(no, {})
            share, market, odds = num(mh.get('model_share')), num(mh.get('market_share')), num(mh.get('win_odds_t10'))
            cancelled = mh.get('scratched') is True
            price_valid = valid and not cancelled and odds is not None and odds > 0
            going = r.get('going')
            if isinstance(going, dict):
                going = going.get('surface_going') or going.get('dirt' if r.get('surface') == 'dirt' else 'turf')
            pace = ((mh.get('pace_shape_predicted') or {}).get('predicted_curve') or {})
            pace_probs = pace.get('pace_probs') or {}
            provenance = r.get('model_provenance') or {}
            main_rows.append({
                '取得日時': captured.isoformat(), '事前予想有効': int(price_valid),
                '発走予定時刻': r.get('post_time', ''), 'オッズ基準日時': clock.isoformat() if clock else '不明',
                'オッズ経過分': elapsed, 'モデル更新日時': raw_data.get('generated_at', '不明'),
                '日付': target_date, '場名': r.get('venue'), 'R': race_no,
                'レース名': r_name or r.get('race_name') or f'第{race_no}競走', '元レースID': raw_id,
                '距離': r.get('distance_m', ''), '芝ダ': r.get('surface', ''), '馬場状態': going or '不明',
                'Hペース率(%)': rounded(pace_probs.get('H'), 1, 100),
                'Sペース率(%)': rounded(pace_probs.get('S'), 1, 100),
                '先行争い圧力': rounded(pace.get('pressure_score')),
                '枠': nkh.get('枠', ''), '馬番': no, '馬名': mh.get('name', ''),
                '性齢': mh.get('sex_age', ''), '斤量': mh.get('carried_weight', ''),
                '騎手': nkh.get('騎手') or mh.get('jockey_changed_to') or '',
                '調教師': nkh.get('調教師', ''), '人気': mh.get('popularity', ''),
                '単勝オッズ': odds if odds is not None else '', '複勝オッズ帯': mh.get('place_odds_t10', ''),
                '損益分岐勝率(%)': round(100 / odds, 2) if odds and odds > 0 else '',
                'AI能力順位': mh.get('strength_rank', ''), 'AI勝率(%)': rounded(share, 2, 100),
                '市場勝率(%)': rounded(market, 2, 100),
                '妙味度(歪み)': rounded(mh.get('divergence'), 3, 100),
                '期待値(EV)': round(share * odds, 4) if price_valid and share is not None else '',
                '複勝率(%)': rounded(mh.get('pl_place_prob'), 2, 100),
                '脚質': mh.get('style_label', ''), '位置取り指数': mh.get('style_early_position', ''),
                '馬体重': mh.get('bodyweight_kg', ''), '増減': mh.get('bodyweight_delta_kg', ''),
                '体重評価スコア': rounded(mh.get('bw_response_score'), 4),
                '開催場名': VENUES.get(int(r.get('venue', 0)), str(r.get('venue', ''))),
                '取消・除外': int(cancelled), 'オッズ時刻採用方法': clock_source,
                'API原オッズ基準日時': r.get('odds_snapshot_at') or '不明',
                '分析状態': state, 'API生成日時': raw_data.get('generated_at', ''),
                'モデルID': provenance.get('model_id') or raw_data.get('model_id', ''),
                'モデル情報': json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                '市場入力の有無': '未確認', 'モデル確率検証': '未検証',
                'late補完項目': ','.join(fallback_fields),
            })
            for past in h.get('past_runs') or []:
                lap = past.get('lap_profile') or {}
                laps = lap.get('laps', []) if isinstance(lap, dict) else []
                late3 = num(past.get('late_3f_sec'))
                history_rows.append({
                    '元レースID': raw_id, '馬番': no, '馬名': h.get('name', ''),
                    '出走日': past.get('date'), '距離': past.get('distance_m'),
                    '芝ダ': past.get('surface') or '不明', '馬場状態': past.get('going') or '不明',
                    '着順': past.get('finish_order'), '頭数': past.get('field_size'),
                    '上がり3F': late3, '上がり3F暫定使用可': int(late3 is not None and late3 >= 25),
                    '上がり優位度': past.get('late_3f_advantage'),
                    '通過順比率': past.get('corner_ratio'),
                    '実測ラップ配列': json.dumps(laps, ensure_ascii=False),
                })
        # No stale late fallback for probabilities: every weight must be present in
        # the same canonical races snapshot. Reject rather than create mixed models.
        try:
            probabilities = ticket_probabilities(r, tau=1.0)
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            warnings.append(f'{raw_id}: 組み合わせ確率算出不可 ({exc})')
            continue
        active = {int(h['horse_no']): h for h in h_list if h.get('scratched') is not True}
        # JRA place payout count follows starters at sales opening. Do not infer
        # it from post-scratch active count; flag cancellations for manual checking.
        if not any(h.get('scratched') is True for h in h_list):
            n = len(h_list)
            if n >= 5:
                target_kind = '3着内' if n >= 8 else '2着内'
                for no in active:
                    probabilities[('複勝', (no,))] = probabilities[(target_kind, (no,))]
        frames = {no: nk_horses.get(no, {}).get('枠') for no in active}
        if len(active) >= 9 and all(isinstance(v, int) and 1 <= v <= 8 for v in frames.values()):
            from collections import defaultdict
            frame_probs = defaultdict(float)
            for (kind, selection), prob in probabilities.items():
                if kind == '馬単':
                    frame_probs[tuple(sorted(frames[n] for n in selection))] += prob
            for selection, prob in frame_probs.items():
                probabilities[('枠連', selection)] = prob
        quinella = {}
        for item in r.get('quinella_odds') or []:
            if not isinstance(item, list) or len(item) != 3:
                continue
            a, b, q = item
            q = num(q)
            if q and q > 0:
                key = tuple(sorted((int(a), int(b))))
                if key in quinella and quinella[key] != q:
                    raise ValueError(f'馬連価格重複不一致: {raw_id} {key}')
                quinella[key] = q
        for (kind, selection), probability in sorted(probabilities.items()):
            price = None
            price_clock, price_source, price_clock_ok = None, '価格未取得', False
            if kind == '単勝':
                price = num(active[selection[0]].get('win_odds_t10'))
                price_clock, price_source, price_clock_ok = clock, clock_source, clock_ok
            elif kind == '馬連':
                price = quinella.get(selection)
                if price:
                    price_clock, price_source, price_clock_ok = odds_clock(r.get('quinella_snapshot_at'), captured)
            elif kind == '複勝':
                text = str(active[selection[0]].get('place_odds_t10') or '')
                values = re.findall(r'\d+(?:\.\d+)?', text)
                if len(values) in (1, 2):
                    price = min(map(float, values))
                    price_clock, price_source, price_clock_ok = clock, clock_source, clock_ok
            priced = bool(valid and price_clock_ok and price is not None and price > 0)
            status = '参考モデルEV算出可・未検証' if priced else '価格未取得または時刻無効'
            if not valid:
                status = '事前評価不可'
            if kind in ('3着内', '2着内'):
                status = '入着率・発売券種ではない'
            combo_rows.append({
                '取得日時': captured.isoformat(), '日付': target_date, '元レースID': raw_id,
                '場名': r.get('venue'), 'R': race_no, '券種': kind,
                '組み合わせ': '-'.join(map(str, selection)),
                '推定的中確率': probability, '推定的中確率(%)': probability * 100,
                '損益分岐オッズ': 1 / probability,
                '提示オッズ': price if price and price > 0 else '',
                'オッズ基準日時': price_clock.isoformat() if price_clock else '',
                'オッズ時刻採用方法': price_source,
                'EV倍率': probability * price if priced else '',
                '期待収益率(%)': (probability * price - 1) * 100 if priced else '',
                '事前予想有効': int(valid), '算出状態': status,
                '確率算出方式': 'PL_exact_top3_v1', 'tau': 1.0,
                'モデルID': (r.get('model_provenance') or {}).get('model_id') or raw_data.get('model_id', ''),
                'API生成日時': raw_data.get('generated_at', ''),
                '確率検証': '未検証', '市場入力の有無': '未確認',
            })
    files = [
        {'filename': f'keiba_integrated_{target_date}.csv', 'content': csv_string(main_rows)},
        {'filename': f'horse_history_{target_date}.csv', 'content': csv_string(history_rows)},
        {'filename': f'keiba_combinations_{target_date}.csv', 'content': csv_string(combo_rows)},
    ]
    return files, {'races': len(seen_races), 'horses': len(main_rows),
                   'history_rows': len(history_rows), 'combination_rows': len(combo_rows),
                   'warnings': warnings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('date', nargs='?')
    parser.add_argument('--input-json', help='ローカル検証用。指定時は送信禁止')
    parser.add_argument('--skip-netkeiba', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output-dir', default='output')
    args = parser.parse_args()
    date = args.date or os.environ.get('TARGET_DATE') or datetime.now(JST).date().isoformat()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise ValueError('対象日は YYYY-MM-DD')
    datetime.strptime(date, '%Y-%m-%d')
    if args.input_json:
        with open(args.input_json, encoding='utf-8-sig') as f:
            raw = json.load(f)
    else:
        raw = fetch_json(date)
    captured = datetime.now(JST)
    def card_fetcher(race_id):
        if args.skip_netkeiba:
            return '', {}
        result = fetch_netkeiba_details(race_id)
        time.sleep(0.2)
        return result
    files, summary = build_files(raw, date, captured, card_fetcher)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for file in files:
        (output / file['filename']).write_text(file['content'], encoding='utf-8-sig')
    print(json.dumps(summary, ensure_ascii=False))
    if args.dry_run or args.input_json:
        print('ローカル検証完了。GAS送信なし。')
        return
    gas_url = os.environ.get('GAS_WEBHOOK_URL')
    if not gas_url:
        raise ValueError('GAS_WEBHOOK_URL未設定')
    req = urllib.request.Request(gas_url, data=json.dumps({'files': files}, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        reply = response.read().decode('utf-8', errors='replace')
    try:
        result = json.loads(reply)
    except ValueError:
        raise ValueError('GAS応答がJSONではありません。保存完了を確認できません') from None
    if not isinstance(result, dict) or (result.get('success') is not True and
                                      result.get('status') not in ('success', 'ok')):
        raise ValueError('GAS保存成功を確認できません。GAS側の応答形式を確認してください')
    print('GASが保存成功を返しました: ' + ', '.join(f['filename'] for f in files))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'処理失敗: {type(exc).__name__}', file=sys.stderr)
        raise SystemExit(1)
