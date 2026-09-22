#!/usr/bin/env python3
"""Python 3.11+. Snapshot-preserving extraction; no betting decisions."""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

JST = timezone(timedelta(hours=9))
VENUES = dict(zip(range(1, 11), ['札幌','函館','福島','新潟','東京','中山','中京','京都','阪神','小倉']))
NA = (None, '', '-', 'None', 'null', 'N/A')


def now():
    return datetime.now(JST).isoformat(timespec='seconds')


def number(v):
    if v in NA or isinstance(v, bool):
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def integer(v):
    n = number(v)
    return int(n) if n is not None and n.is_integer() else None


def js(v):
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'))


def timestamp(raw):
    """Never invent a timezone or attach a capture date to a clock-only value."""
    if not isinstance(raw, str):
        return None
    try:
        t = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return t if t.tzinfo is not None else None
    except ValueError:
        return None


def json_write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def csv_write(path, rows, minimum):
    cols = list(dict.fromkeys(minimum + [k for r in rows for k in r]))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header('Cookie') and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            raise ValueError('cross_host_cookie_redirect')
        if urlparse(newurl).scheme != 'https':
            raise ValueError('non_https_redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_bytes(url, cookie='', data=None):
    if urlparse(url).scheme != 'https':
        raise ValueError('https_required')
    headers = {'User-Agent': 'Mozilla/5.0'}
    if cookie:
        headers['Cookie'] = cookie
    if data is not None:
        headers['Content-Type'] = 'application/json'
    # Deliberately no automatic POST retry: a timeout may occur after a successful save.
    with build_opener(SafeRedirect()).open(Request(url, data=data, headers=headers), timeout=30) as r:
        raw = r.read(30_000_001)
        if len(raw) > 30_000_000:
            raise ValueError('response_too_large')
        return raw, r.headers, r.geturl()


class TableParser(HTMLParser):
    """Read table cells and descendant classes; tolerate extra class names/attributes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.texts = [], None, None, []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th') and self.row is not None:
            self.cell = {'classes': set(), 'parts': []}
        if self.cell is not None:
            self.cell['classes'].update(a.get('class', '').split())
    def handle_data(self, data):
        if not self.hidden:
            self.texts.append(data)
            if self.cell is not None:
                self.cell['parts'].append(data)
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden-1)
        if self.hidden:
            return
        if tag in ('td', 'th') and self.cell is not None:
            self.cell['text'] = ' '.join(' '.join(self.cell['parts']).split())
            self.row.append(self.cell)
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def cell_text(row, cls):
    hits = [c['text'] for c in row if cls in c['classes']]
    return hits[0] if len(hits) == 1 else None


def parse_netkeiba(html, expected, kind):
    p = TableParser()
    p.feed(html)
    text = ' '.join(p.texts)
    found, duplicate = {}, set()
    for row in p.rows:
        n = integer(cell_text(row, 'Umaban'))
        if n is None:
            continue
        if n in found:
            duplicate.add(n)
        entry = {'horse_name': cell_text(row, 'HorseName')}
        if kind == 'shutuba':
            entry.update(waku=integer(cell_text(row, 'Waku')), jockey=cell_text(row, 'Jockey'),
                         trainer=cell_text(row, 'Trainer'))
        elif kind == 'oikiri':
            evaluation = cell_text(row, 'Evaluation')
            ranks = [c['text'] for c in row if any(s.startswith('Rank_') for s in c['classes'])]
            # Rating is extracted only from an unambiguous A/B/C token, never guessed from class suffix.
            tokens = re.findall(r'(?<![A-Za-z])[ABC](?![A-Za-z])', ' '.join(ranks))
            entry.update(training_rating=tokens[0] if len(tokens) == 1 else None,
                         training_comment=evaluation)
        else:
            entry['stable_comment'] = cell_text(row, 'Comment_Text')
        found[n] = entry
    result = {}
    for n, name in expected.items():
        entry = found.get(n, {})
        # No horse-name match => do not merge paid information on horse number alone.
        label = entry.get('horse_name')
        matched = bool(label and name in label)
        status = 'parsed' if matched and n not in duplicate else 'identity_unverified'
        required = {'shutuba':['jockey','trainer','waku'], 'oikiri':['training_rating','training_comment'],
                    'comment':['stable_comment']}[kind]
        if not entry:
            if any(s in text for s in ['公開予定', '公開前']):
                status = 'not_published'
            elif any(s in text for s in ['公開の対象', '対象外']):
                status = 'not_applicable_or_unverified'
            else:
                status = 'unparsed_or_auth_required'
        elif matched and any(entry.get(k) in NA for k in required):
            status = 'partial'
        if any(s in ' '.join(str(entry.get(k, '')) for k in required) for s in ['公開予定','---','ログイン']):
            status = 'placeholder'
        result[n] = {'status': status, **(entry if matched and n not in duplicate else {})}
    return result


def fetch_netkeiba(nid, expected, cookie, delay):
    result = {n:{} for n in expected}
    for kind in ['shutuba','oikiri','comment']:
        if kind != 'shutuba' and not cookie:
            parsed = {n:{'status':'cookie_not_configured'} for n in expected}
        else:
            time.sleep(delay)
            try:
                raw, headers, final = request_bytes(f'https://race.netkeiba.com/race/{kind}.html?race_id={nid}', cookie)
                if 'login' in final.lower():
                    parsed = {n:{'status':'auth_required'} for n in expected}
                else:
                    enc = headers.get_content_charset() or 'euc-jp'
                    parsed = parse_netkeiba(raw.decode(enc, errors='replace'), expected, kind)
            except HTTPError as e:
                parsed = {n:{'status':f'http_{e.code}'} for n in expected}
            except Exception as e:
                parsed = {n:{'status':'fetch_error_' + type(e).__name__} for n in expected}
        for n in expected:
            result[n][kind] = {**parsed[n], 'captured_at':now()}
    return result


def transform(raw, target, captured, nk=None, snapshot_id='', max_age=15):
    races = raw.get('races')
    if not isinstance(races, list):
        raise ValueError('races_must_be_list')
    late_root = raw.get('late') or {}
    late = late_root.get('races', {}) if isinstance(late_root, dict) else {}
    if not isinstance(late, dict):
        raise ValueError('late_races_must_be_mapping')
    main, history, issues, seen = [], [], [], set()
    def issue(rid, no, code):
        issues.append({'race_id':rid,'horse_no':no,'code':code})
    for r in races:
        rid = r.get('race_id', '')
        m = re.fullmatch(r'(\d{4})_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})', rid)
        if not m:
            issue(rid,None,'invalid_race_id'); continue
        year, md, venue, meeting, day, rn = m.groups()
        try:
            id_day = date.fromisoformat(f'{year}-{md[:2]}-{md[2:]}')
        except ValueError:
            issue(rid,None,'invalid_race_date'); continue
        nid = year+venue+meeting+day+rn
        race_errors = []
        if id_day.isoformat() != target:
            race_errors.append('race_id_date_mismatch')
        # A postponement requires a verified mapping; do not silently rewrite IDs.
        horses = r.get('horses')
        if not isinstance(horses, list) or not horses:
            issue(rid,None,'no_horses'); continue
        late_items = late.get(rid, [])
        if not isinstance(late_items, list):
            issue(rid,None,'unsupported_late_schema'); late_items=[]
            race_errors.append('unsupported_late_schema')
        lm = {}
        for x in late_items:
            no = integer(x.get('horse_no'))
            if no in lm:
                race_errors.append('duplicate_late_horse')
            lm[no] = x
        expected = {integer(h.get('horse_no')):h.get('name','') for h in horses}
        if None in expected or len(expected) != len(horses):
            issue(rid,None,'invalid_or_duplicate_horse_number'); continue
        if any(n not in expected for n in lm):
            race_errors.append('unknown_late_horse')
        verified_nk = nk(nid, expected) if nk and not race_errors else {}
        race_rows=[]
        for h in horses:
            no = integer(h.get('horse_no'))
            key = (rid,no)
            if key in seen:
                issue(rid,no,'duplicate_primary_key'); continue
            seen.add(key)
            errors = list(race_errors)
            lh = lm.get(no)
            src = lh if lh is not None else h
            source = 'late' if lh is not None else 'morning'
            # All pricing/model fields use the same source group, including explicit nulls.
            def val(k): return src.get(k)
            p, odds = number(val('model_share')), number(val('win_odds_t10'))
            pp = number(val('pl_place_prob'))
            snapshot = src.get('odds_snapshot_at') if source=='late' else src.get('odds_snapshot_at',r.get('odds_snapshot_at'))
            received = src.get('odds_received_at') if source=='late' else src.get('odds_received_at',r.get('odds_received_at'))
            # Race-level timestamp is preserved even when scope to a late record is unconfirmed.
            st = timestamp(snapshot)
            ct = timestamp(captured)
            if ct is None: errors.append('capture_time_unknown')
            age = (ct-st).total_seconds()/60 if st and ct else None
            if st is None: errors.append('odds_timestamp_missing_or_timezone_unknown')
            elif age is not None and age < -1: errors.append('odds_timestamp_in_future')
            elif age is not None and age > max_age: errors.append('stale_odds')
            post = r.get('post_time')
            post_dt = None
            if isinstance(post,str) and re.fullmatch(r'\d{2}:\d{2}',post):
                try: post_dt=datetime.fromisoformat(target+'T'+post+':00+09:00')
                except ValueError: pass
            if post_dt is None: errors.append('post_time_unknown')
            elif (ct and ct>=post_dt) or (st and st>=post_dt): errors.append('post_race_snapshot')
            if p is None or not 0<=p<=1: errors.append('invalid_model_probability'); p=None
            if odds is None or odds<1: errors.append('invalid_odds'); odds=None
            if pp is not None and not 0<=pp<=1: errors.append('invalid_place_probability'); pp=None
            ev = p*odds if p is not None and odds is not None else None
            going = r.get('going')
            surface = r.get('surface')
            going_value = going.get('surface_going') if isinstance(going,dict) else going
            if going_value is None and isinstance(going,dict) and surface in ('turf','dirt'):
                going_value=going.get(surface)
            pred=(h.get('pace_shape_predicted') or {}).get('predicted_curve') or {}
            probs=pred.get('pace_probs') or {}
            runs=h.get('past_runs')
            if runs is None: issue(rid,no,'past_runs_missing')
            elif not isinstance(runs,list): errors.append('past_runs_invalid'); runs=[]
            for idx, past in enumerate(runs or [],1):
                pd = past.get('date')
                try:
                    eligible=date.fromisoformat(pd)<date.fromisoformat(target)
                except (ValueError,TypeError): eligible=False
                if not eligible: issue(rid,no,'history_date_not_before_target')
                lp=past.get('lap_profile') or {}
                late3=number(past.get('late_3f_sec'))
                unit_ok=late3 is not None and late3>=20
                history.append({'snapshot_id':snapshot_id,'race_id':rid,'horse_no':no,
                    'horse_name':h.get('name'),'history_index':idx,'past_date':pd,
                    'eligible_pre_race':eligible,'distance_m':past.get('distance_m'),
                    'surface':past.get('surface'),'going':past.get('going'),
                    'finish_order':past.get('finish_order'),'field_size':past.get('field_size'),
                    'late_3f_sec_raw':past.get('late_3f_sec'),
                    'late_3f_sec_for_analysis':late3 if unit_ok else None,
                    'late_3f_status':'plausibility_only_not_unit_verified' if unit_ok else 'unit_or_value_unverified',
                    'late_3f_advantage':past.get('late_3f_advantage'),'corner_ratio':past.get('corner_ratio'),
                    'laps_json':js(lp.get('laps')),'lap_profile_json':js(lp),'past_run_raw_json':js(past)})
            info=verified_nk.get(no,{})
            row={'snapshot_id':snapshot_id,'race_id':rid,'netkeiba_race_id':nid,'日付':target,
                '場名':VENUES.get(int(venue),venue),'R':int(rn),'レース名':r.get('race_name'),
                '馬番':no,'馬名':h.get('name'),'発走':post,'距離':r.get('distance_m'),
                'surface':surface,'馬場':going_value,'going_raw_json':js(going),
                'captured_at':captured,'odds_snapshot_at':snapshot,'odds_received_at':received,
                'race_odds_snapshot_at_raw':r.get('odds_snapshot_at'),
                'race_odds_received_at_raw':r.get('odds_received_at'),
                'model_generated_at':src.get('model_generated_at'),
                'root_generated_at_raw':raw.get('generated_at'),
                'odds_age_minutes':round(age,2) if age is not None else None,'value_source':source,
                'AI勝率(%)':p*100 if p is not None else None,'単勝オッズ':odds,
                'expected_return_multiple':ev,'expected_net_roi':ev-1 if ev is not None else None,
                'model_market_share':number(val('market_share')),'divergence':number(val('divergence')),
                '複勝率(%)':pp*100 if pp is not None else None,'複勝オッズ帯':val('place_odds_t10'),
                'morning_place_probability_raw':h.get('pl_place_prob'),
                'AI能力順位':h.get('strength_rank'),'mark_morning':h.get('mark_morning'),
                'mark_late':src.get('mark_late'),'性齢':h.get('sex_age'),'斤量':h.get('carried_weight'),
                '脚質':h.get('style_label'),'位置取り指数':h.get('style_early_position'),
                '馬体重':val('bodyweight_kg'),'増減':val('bodyweight_delta_kg'),
                '体重評価スコア':val('bw_response_score'),
                'Hペース率':number(probs.get('H')),'Mペース率':number(probs.get('M')),
                'Sペース率':number(probs.get('S')),'先行争い圧力':number(pred.get('pressure_score')),
                'pace_scope':'horse_predicted_curve','past_runs_count':len(runs) if isinstance(runs,list) else None,
                'market_in_model':'unconfirmed','bw_in_model':'unconfirmed',
                'analysis_status':'REVIEW_REQUIRED','validation_errors':';'.join(sorted(set(errors)))}
            for kind in ['shutuba','oikiri','comment']:
                nki=info.get(kind,{'status':'not_requested'})
                row[kind+'_status']=nki['status']
                for k,v in nki.items():
                    if k!='status': row[kind+'_'+k]=v
            for code in errors: issue(rid,no,code)
            main.append(row);race_rows.append(row)
        ps=[x['AI勝率(%)'] for x in race_rows]
        if ps and (any(p is None for p in ps) or abs(sum(p for p in ps if p is not None)-100)>.1):
            for x in race_rows:
                x['validation_errors']+=';probability_sum_not_100'
                issue(rid,x['馬番'],'probability_sum_not_100')
    return main,history,issues


def upload_gas(url, path):
    payload={'filename':path.name,'content':path.read_text(encoding='utf-8-sig')}
    raw,_,_=request_bytes(url,data=json.dumps(payload).encode('utf-8'))
    # HTTP 200 alone does not demonstrate a Drive save.
    try: ack=json.loads(raw)
    except ValueError: raise ValueError('gas_ack_not_json')
    if not isinstance(ack,dict) or ack.get('success') is not True or ack.get('filename')!=path.name:
        raise ValueError('gas_save_not_confirmed')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('target_date',nargs='?',default=datetime.now(JST).date().isoformat())
    ap.add_argument('--input',type=Path,help='ローカル生JSONで検証。ネット接続不要')
    ap.add_argument('--output',type=Path,default=Path('output'))
    ap.add_argument('--netkeiba',action='store_true')
    ap.add_argument('--upload',action='store_true')
    ap.add_argument('--max-odds-age-minutes',type=float,default=15)
    ap.add_argument('--request-interval',type=float,default=1)
    args=ap.parse_args()
    date.fromisoformat(args.target_date)
    if args.max_odds_age_minutes<=0 or args.request_interval<.5:
        raise ValueError('invalid_time_limit')
    fetched_at=now()
    if args.input:
        raw=json.loads(args.input.read_text(encoding='utf-8-sig'))
        captured=None # Offline input has no trustworthy acquisition time; do not invent one.
    else:
        data,_,_=request_bytes(f'https://keiba-simu-euphoria.ef4-db.workers.dev/days/{args.target_date}.json')
        captured=now();raw=json.loads(data)
    if not isinstance(raw,dict): raise ValueError('root_must_be_object')
    sid=datetime.now(JST).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
    out=args.output/sid
    out.mkdir(parents=True,exist_ok=False)
    json_write(out/'raw.json',raw)
    cookie=os.environ.get('NETKEIBA_COOKIE','')
    nk=(lambda nid,expected:fetch_netkeiba(nid,expected,cookie,args.request_interval)) if args.netkeiba else None
    main_rows,hist,issues=transform(raw,args.target_date,captured,nk,sid,args.max_odds_age_minutes)
    csv_write(out/'keiba_integrated.csv',main_rows,['snapshot_id','race_id','馬番','馬名'])
    csv_write(out/'horse_history.csv',hist,['snapshot_id','race_id','horse_no','history_index'])
    csv_write(out/'validation_issues.csv',issues,['race_id','horse_no','code'])
    files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in out.iterdir() if f.is_file()}
    manifest={'schema_version':2,'snapshot_id':sid,'target_date':args.target_date,
        'processed_at':fetched_at,'captured_at':captured,'races_in_source':len(raw.get('races',[])),
        'main_rows':len(main_rows),'history_rows':len(hist),'issue_count':len(issues),
        'files_sha256':files,'analysis_status':'REVIEW_REQUIRED',
        'status':'exported_with_issues' if issues else 'exported',
        'note':'No BUY signal. No assertion of calibrated model or independent probabilities.'}
    json_write(out/'manifest.json',manifest)
    print(f'Exported {len(main_rows)} horses / {len(hist)} past runs / {len(issues)} issues: {out}')
    if args.upload:
        url=os.environ.get('GAS_WEBHOOK_URL')
        if not url: raise ValueError('GAS_WEBHOOK_URL_missing')
        # Run-specific names prevent unrelated/mixed snapshots from overwriting each other.
        manifest['uploaded_files_sha256'] = {sid+'_'+name: files[name] for name in ['keiba_integrated.csv','horse_history.csv','validation_issues.csv']}
        json_write(out/'manifest.json',manifest)
        for base in ['keiba_integrated.csv','horse_history.csv','validation_issues.csv','manifest.json']:
            upload_path=out/(sid+'_'+base)
            upload_path.write_bytes((out/base).read_bytes())
            upload_gas(url,upload_path)
        print('GAS confirmed all four files; raw.json remains a local artifact.')
    return 2 if issues or not main_rows else 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError,OSError,HTTPError,URLError,KeyError,TypeError) as e:
        # Exception text can contain a secret webhook URL. Never print it.
        print('Pipeline failed: '+type(e).__name__+'; inspect local outputs/configuration.',file=sys.stderr)
        sys.exit(1)
