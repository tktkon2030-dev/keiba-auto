"""Immutable pre-race snapshots and honest post-race diagnostics (stdlib only)."""
import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from keiba_scraper import JST, timestamp
from postrace_results import SIZES, fetch_result, selection


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def positive(value):
    try:
        v = float(value)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def capture(horses, candidates):
    """Retain whole races from one pre-race capture; never merge horse versions."""
    grouped = defaultdict(list)
    for row in horses:
        grouped[row['元レースID']].append(row)
    races, rejected = [], []
    for rid, all_rows in grouped.items():
        rows = [r for r in all_rows if str(r.get('取消・除外', '0')) in ('0', '', 'False')]
        try:
            if len(rows) < 3:
                raise ValueError('insufficient active horses')
            dates = {r['日付'] for r in rows}
            captures = {r['取得日時'] for r in rows}
            model_signatures = {canonical({k: v for k, v in r.items() if k in (
                'モデルID', 'モデル情報', 'モデル選択方式', 'モデル勝率項目',
                '採用項目', '選択方式', '確率算出方式', 'tau')})
                                for r in rows}
            if len(dates) != 1 or len(captures) != 1 or len(model_signatures) != 1:
                raise ValueError('mixed capture/model')
            first = rows[0]
            if any(r.get('API生成日時') != first.get('API生成日時') or
                   r.get('発走予定時刻') != first.get('発走予定時刻') for r in rows):
                raise ValueError('mixed source generation/post time')
            captured = timestamp(first['取得日時'])
            post = timestamp(first['発走予定時刻'], first['日付'])
            generated = timestamp(first.get('API生成日時'))
            if not captured or not post or not generated or not generated <= captured < post:
                raise ValueError('not a provable pre-race snapshot')
            if any(str(r['事前予想有効']) != '1' for r in rows):
                raise ValueError('ineligible race snapshot')
            if len({r['馬番'] for r in rows}) != len(rows):
                raise ValueError('duplicate horse')
            probabilities = {r['馬番']: positive(r['AI勝率(%)']) for r in rows}
            if any(p is None or p > 100 for p in probabilities.values()):
                raise ValueError('invalid probability')
            if abs(sum(probabilities.values()) - 100) > .2:
                raise ValueError('probability mass invalid')
            model_key = digest(json.loads(next(iter(model_signatures))))
            race = {'race_id': rid, 'date': first['日付'], 'captured_at': captured.isoformat(),
                    'post_at': post.isoformat(), 'model_key': model_key,
                    'model_id': first.get('モデルID', '不明'),
                    'venue': first.get('開催場名', ''), 'race_name': first.get('レース名', ''),
                    'going': first.get('馬場状態', '不明'),
                    'horses': rows,
                    'candidates': [r for r in candidates if r['元レースID'] == rid and
                                   r['取得日時'] == first['取得日時']]}
            race['snapshot_id'] = digest(race)
            races.append(race)
        except (ValueError, KeyError, TypeError) as exc:
            rejected.append({'race_id': rid, 'reason': str(exc)})
    return {'kind': 'keiba_pre_race_snapshots_v1', 'races': races, 'rejected': rejected}


def verify_bundle(bundle):
    for race in bundle.get('races', []):
        original = dict(race)
        sid = original.pop('snapshot_id', '')
        if digest(original) != sid:
            raise ValueError('snapshot checksum mismatch')
    return bundle


def latest_snapshots(bundles):
    latest = {}
    for bundle in bundles:
        verify_bundle(bundle)
        for race in bundle['races']:
            captured, post = timestamp(race['captured_at']), timestamp(race['post_at'])
            if not captured or not post or captured >= post:
                raise ValueError('snapshot after post')
            old = latest.get(race['race_id'])
            if not old or (race['captured_at'], race['snapshot_id']) > (old['captured_at'], old['snapshot_id']):
                latest[race['race_id']] = race
    return latest


def race_observation(race, result):
    """Categorical win scoring excludes ties and late-scratch field changes."""
    finish = {int(n): v for n, v in result['finish'].items()}
    field = {int(h['馬番']) for h in race['horses']}
    active = set(finish) - set(result['scratched'])
    winners = [n for n, v in finish.items() if v == 1]
    if active != field or len(winners) != 1:
        raise ValueError('field changed or dead heat; win scoring withheld')
    if len(result.get('payouts', {}).get('単勝', {})) != 1:
        raise ValueError('win payout unavailable')
    probs = {int(h['馬番']): float(h['AI勝率(%)']) / 100 for h in race['horses']}
    total = sum(probs.values())
    if abs(total - 1) > .002 or any(p <= 0 or not math.isfinite(p) for p in probs.values()):
        raise ValueError('invalid probability distribution')
    # Only undo export rounding for categorical scoring; original CSV is preserved.
    probs = {n: p / total for n, p in probs.items()}
    return {'race_id': race['race_id'], 'date': race['date'], 'model_key': race['model_key'],
            'model_id': race['model_id'], 'venue': race['venue'], 'going': race['going'],
            'snapshot_id': race['snapshot_id'], 'p': probs, 'winner': winners[0]}


def score(observations, temperature=1):
    if not observations:
        return {'races': 0, 'log_loss': None, 'brier': None, 'top_pick_hit_rate': None}
    losses, briers, hits = [], [], []
    for obs in observations:
        logs = {n: math.log(p) / temperature for n, p in obs['p'].items()}
        maximum = max(logs.values())
        weights = {n: math.exp(p - maximum) for n, p in logs.items()}
        total = sum(weights.values())
        p = {n: w / total for n, w in weights.items()}
        winner = obs['winner']
        losses.append(-math.log(max(p[winner], 1e-15)))
        briers.append(sum((v - int(n == winner)) ** 2 for n, v in p.items()))
        pick = min(p, key=lambda n: (-p[n], n))
        hits.append(int(pick == winner))
    return {'races': len(observations), 'log_loss': sum(losses) / len(losses),
            'brier': sum(briers) / len(briers), 'top_pick_hit_rate': sum(hits) / len(hits)}


def calibration_bins(observations):
    bins = defaultdict(list)
    for obs in observations:
        for no, p in obs['p'].items():
            bins[min(int(p * 10), 9)].append((p, int(no == obs['winner'])))
    return [{'probability_band': f'{b * 10}-{(b + 1) * 10}%', 'horses': len(items),
             'mean_prediction': sum(p for p, _ in items) / len(items),
             'observed_win_rate': sum(y for _, y in items) / len(items)}
            for b, items in sorted(bins.items())]


def propose_temperature(observations):
    """A research proposal only. Never silently change CSV probabilities."""
    obs = sorted(observations, key=lambda r: (r['date'], r['race_id']))
    # Split on dates, not individual horses or races on the same day.
    dates = sorted({o['date'] for o in obs})
    if len(obs) < 200 or len(dates) < 6:
        return {'status': 'insufficient_history', 'races': len(obs), 'auto_applied': False}
    cutoff = dates[max(1, int(len(dates) * .8))]
    train = [o for o in obs if o['date'] < cutoff]
    holdout = [o for o in obs if o['date'] >= cutoff]
    if len(train) < 150 or len(holdout) < 50:
        return {'status': 'insufficient_date_holdout', 'races': len(obs), 'auto_applied': False}
    grid = [.7, .85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0]
    best = min(grid, key=lambda t: (score(train, t)['log_loss'], abs(t - 1)))
    before, after = score(holdout), score(holdout, best)
    improved = (best != 1 and after['log_loss'] < before['log_loss'] and
                after['brier'] <= before['brier'])
    return {'status': 'review_candidate' if improved else 'no_improvement',
            'temperature': best, 'train_races': len(train), 'holdout_races': len(holdout),
            'holdout_start_date': cutoff, 'trained_through': max(o['date'] for o in train),
            'holdout_before': before, 'holdout_after': after, 'auto_applied': False,
            'note': 'research validation only; a further untouched future period is required'}


def settle(kind, combo, stake, result):
    """Returns (state, payout). Missing result is never a loss; frames are cautious."""
    combo = selection(kind, combo)
    scratched = set(map(int, result.get('scratched', [])))
    if kind == '枠連' and scratched:
        return 'refund_unverified', None
    if kind != '枠連' and scratched.intersection(map(int, combo.split('-'))):
        return 'refunded', stake
    quotes = result['payouts'].get(kind)
    if quotes is None:
        return 'payout_missing', None
    return ('hit', quotes[combo] * stake // 100) if combo in quotes else ('miss', 0)


def summarize_settlements(rows):
    out = {}
    for kind in SIZES:
        group = [r for r in rows if r['bet_type'] == kind]
        decided = [r for r in group if r['state'] in ('hit', 'miss')]
        stake = sum(r['stake_yen'] for r in decided)
        payout = sum(r['payout_yen'] for r in decided)
        out[kind] = {'tickets': len(decided), 'stake_yen': stake, 'payout_yen': payout,
                     'roi': payout / stake if stake else None,
                     'hit_rate': sum(r['state'] == 'hit' for r in decided) / len(decided) if decided else None,
                     'refunded_yen': sum(r['stake_yen'] for r in group if r['state'] == 'refunded'),
                     'unresolved': sum(r['state'] not in ('hit', 'miss', 'refunded') for r in group)}
    return out


def evaluate(snapshots, results, ledger=None):
    observations, excluded, simulation, actual = [], [], [], []
    ticket_observations = defaultdict(list)
    for rid, race in sorted(snapshots.items()):
        result = results.get(rid)
        if result is None:
            excluded.append({'race_id': rid, 'reason': 'result unavailable'})
            continue
        field = {int(h['馬番']) for h in race['horses']}
        if not field.issubset(set(map(int, result['finish']))):
            excluded.append({'race_id': rid, 'reason': 'result field incomplete'})
            continue
        try:
            observations.append(race_observation(race, result))
        except ValueError as exc:
            excluded.append({'race_id': rid, 'reason': str(exc)})
        for t in race['candidates']:
            kind, combo = t['券種'], t['組み合わせ']
            p = positive(t['推定的中確率(%)'])
            outcome, _ = settle(kind, combo, 100, result)
            if p and p < 100 and outcome in ('hit', 'miss'):
                ticket_observations[kind].append((p / 100, int(outcome == 'hit')))
            # Freeze the pre-race selection; never select with final odds.
            if t['判定'] != '購入検討':
                continue
            if kind == '複勝' and (positive(t['提示オッズ']) or 0) < 10:
                continue
            state, payout = settle(kind, combo, 100, result)
            simulation.append({'race_id': rid, 'snapshot_id': race['snapshot_id'],
                               'bet_type': kind, 'selection': combo, 'stake_yen': 100,
                               'state': state, 'payout_yen': payout})
    seen = set()
    for bet in ledger or []:
        bid, rid, kind = bet['purchase_id'], bet['race_id'], bet['bet_type']
        if not bid or bid in seen:
            raise ValueError('missing/duplicate purchase_id')
        seen.add(bid)
        stake = int(bet['stake_yen'])
        if str(stake) != str(bet['stake_yen']) or stake <= 0 or stake % 100:
            raise ValueError('stake must be a positive multiple of 100')
        combo = selection(kind, bet['selection'])
        race = snapshots.get(rid)
        bought = timestamp(bet['purchased_at'])
        # Ledger is evidence of a purchase, not a reconstructed recommendation.
        # A later pre-race snapshot must not invalidate an earlier real purchase.
        if not race or not bought or bought >= timestamp(race['post_at']):
            raise ValueError('purchase time missing or after post')
        result = results.get(rid)
        if result and not {int(h['馬番']) for h in race['horses']}.issubset(set(map(int, result['finish']))):
            result = None
        state, payout = settle(kind, combo, stake, result) if result else ('result_missing', None)
        actual.append({'purchase_id': bid, 'race_id': rid, 'bet_type': kind,
                       'selection': combo, 'stake_yen': stake, 'state': state, 'payout_yen': payout})
    models = defaultdict(list)
    for observation in observations:
        models[observation['model_key']].append(observation)
    grouped = {}
    for field in ('venue', 'going'):
        groups = defaultdict(list)
        for observation in observations:
            groups[observation[field]].append(observation)
        grouped[field] = {key: score(rows) for key, rows in groups.items()}
    ticket_scores = {}
    for kind, items in ticket_observations.items():
        ticket_scores[kind] = {
            'candidate_count': len(items),
            'brier': sum((p - y) ** 2 for p, y in items) / len(items),
            'log_loss': sum(-math.log(max(p if y else 1-p, 1e-15)) for p, y in items) / len(items),
            'mean_prediction': sum(p for p, _ in items) / len(items),
            'hit_rate': sum(y for _, y in items) / len(items),
            'scope': 'shortlisted candidates only; not all combinations'}
    return {'kind': 'keiba_postrace_review_v1', 'scores': score(observations),
            'ticket_probability_scores': ticket_scores,
            'calibration_bins': calibration_bins(observations), 'by_condition': grouped,
            'models': {key: {'model_id': obs[0]['model_id'], 'scores': score(obs),
                             'proposal': propose_temperature(obs)} for key, obs in models.items()},
            'simulation_label': 'pre-race purchase-consideration candidates at flat 100 yen; NOT actual purchases',
            'simulation': summarize_settlements(simulation), 'simulation_rows': simulation,
            'actual': summarize_settlements(actual) if ledger is not None else None,
            'actual_rows': actual, 'excluded': excluded,
            'source_note': 'netkeiba secondary results; official_verified field retained in results archive'}


def load_history(directory):
    bundles, results = [], {}
    for path in sorted(Path(directory).rglob('*.json')):
        obj = json.loads(path.read_text(encoding='utf-8'))
        if obj.get('kind') == 'keiba_pre_race_snapshots_v1':
            bundles.append(obj)
        elif obj.get('kind') == 'keiba_results_v1':
            for rid, result in obj['results'].items():
                if rid not in results or result['fetched_at'] > results[rid]['fetched_at']:
                    results[rid] = result
    return latest_snapshots(bundles), results


def report_markdown(review):
    sc = review['scores']
    text = ['# 競馬・レース後検証', '', f"評価できた発走前予測：{sc['races']}レース。",
            '元の勝率は変更していません。補正案は自動適用しません。',
            '結果はnetkeiba公開情報。主催者照合済みとは扱いません。', '',
            '## 勝率の検証', '', f"Log loss：{sc['log_loss']} / Brier：{sc['brier']}",
            f"能力1位の勝率：{sc['top_pick_hit_rate']}", '',
            '## 候補の100円均等買い試算', '',
            '実購入収支ではありません。発走前に「購入検討」だった候補のみ。',
            '推定オッズだけの候補は除外。欠損・返還確認待ちは損失に数えません。', '',
            '|券種|確定点数|投資額|払戻額|回収率|未解決|', '|---|---:|---:|---:|---:|---:|']
    for kind, row in review['simulation'].items():
        roi = f"{row['roi'] * 100:.1f}%" if row['roi'] is not None else '未算出'
        text.append(f"|{kind}|{row['tickets']}|{row['stake_yen']}|{row['payout_yen']}|{roi}|{row['unresolved']}|")
    text += ['', '## 実購入', '', '購入履歴未入力のため未算出。' if review['actual'] is None
             else json.dumps(review['actual'], ensure_ascii=False, indent=2), '',
             '## 確率補正の研究案', '', json.dumps(review['models'], ensure_ascii=False, indent=2), '',
             f"未評価・除外：{len(review['excluded'])}レース。詳細はreview.json。",
             f"結果取得エラー：{len(review.get('fetch_errors', []))}件。未取得は外れに数えません。",
             'サンプル不足の細分条件や1日の回収率だけでモデル・購入額を変更しません。']
    return '\n'.join(text) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--input-dir', default='output')
    cap.add_argument('--output-dir', default='archive')
    rev = sub.add_parser('review')
    rev.add_argument('--history', required=True)
    rev.add_argument('--output-dir', default='review')
    rev.add_argument('--ledger', help='actual purchases CSV; never inferred from suggestions')
    rev.add_argument('--fetch-results', action='store_true')
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if args.command == 'capture':
        paths = sorted(Path(args.input_dir).glob('keiba_integrated_*.csv'))
        if not paths:
            raise ValueError('integrated CSV not found')
        for path in paths:
            date = path.stem.removeprefix('keiba_integrated_')
            candidate_path = path.with_name(f'keiba_ticket_candidates_{date}.csv')
            bundle = capture(read_csv(path), read_csv(candidate_path))
            content = canonical(bundle) + '\n'
            target = output / f'snapshot-{date}-{digest(bundle)[:16]}.json'
            if target.exists() and target.read_text() != content:
                raise ValueError('immutable snapshot collision')
            if not target.exists():
                with target.open('x', encoding='utf-8') as handle:
                    handle.write(content)
            print(json.dumps({'captured_races': len(bundle['races']),
                              'rejected_races': len(bundle['rejected'])}))
        return
    snapshots, results = load_history(args.history)
    results = {rid: result for rid, result in results.items() if rid in snapshots}
    errors = []
    refreshed = []
    if args.fetch_results:
        now = datetime.now(JST)
        for rid, race in sorted(snapshots.items()):
            # Never fetch a not-yet-finished race. Refresh recent days for corrections.
            post = timestamp(race['post_at'])
            if post + timedelta(hours=2) > now:
                continue
            if rid in results and post < now - timedelta(days=2):
                continue
            try:
                results[rid] = fetch_result(rid)
                refreshed.append(rid)
            except Exception as exc:
                errors.append({'race_id': rid, 'error_type': type(exc).__name__})
    result_bundle = {'kind': 'keiba_results_v1', 'results': results, 'fetch_errors': errors,
                     'refreshed_race_ids': refreshed}
    (output / 'results.json').write_text(canonical(result_bundle) + '\n', encoding='utf-8')
    review = evaluate(snapshots, results, read_csv(args.ledger) if args.ledger else None)
    review['fetch_errors'] = errors
    (output / 'review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'review.md').write_text(report_markdown(review), encoding='utf-8')
    print(json.dumps({'snapshots': len(snapshots), 'scored_races': review['scores']['races'],
                      'fetch_errors': len(errors)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
