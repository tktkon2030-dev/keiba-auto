"""Euphoria day JSON -> ticket probabilities under an explicit PL assumption.

Python standard library only. Integration:
    from euphoria_combinations import export_combinations
    export_combinations(day_json, output_path, tau=1.0)
CLI (reuse the JSON already fetched by the pipeline):
    python euphoria_combinations.py --input day.json --output combinations.csv

This is a reference-model estimate, NOT a calibrated probability or lower bound.
Do not replace independent ability estimates with these external probabilities.
No odds-based fallback, odds fetching, EV calculation, or purchase recommendation.
Site batch simulations use log(model_share)/tau + Gumbel noise. We enumerate
ordered top-three outcomes exactly for the same PL assumption (no sampling error).
API pl_place_prob is preserved separately; it may differ due to sampling/settings.
"""
import argparse
import csv
import itertools
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
FIELDS = ['race_id', 'bet_type', 'selection', 'probability', 'probability_pct',
          'break_even_odds', 'api_pl_place_prob', 'tau', 'method', 'calibration',
          'model_id', 'model_provenance', 'source_generated_at', 'exported_at',
          'post_time', 'race_status', 'odds_snapshot_at', 'odds', 'ev', 'status']


def ticket_probabilities(race, tau=1.0):
    """Return all available marginal/joint probabilities; never multiply marginals."""
    if not math.isfinite(tau) or tau <= 0:
        raise ValueError('tau must be positive and finite')
    horses = [h for h in race['horses'] if h.get('scratched') is not True]
    if len(horses) < 3:
        raise ValueError('at least three active horses required')
    nums = [int(h['horse_no']) for h in horses]
    if len(set(nums)) != len(nums):
        raise ValueError('duplicate horse_no')
    values = [h.get('model_share') for h in horses]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('missing/invalid model_share; no market fallback permitted')
    logs = [math.log(v) / tau for v in values]
    scaled = [math.exp(v - max(logs)) for v in logs]
    total = sum(scaled)
    w = {n: v / total for n, v in zip(nums, scaled)}
    if any(v <= 0 or v >= 1 for v in w.values()):
        raise ValueError('degenerate weights')
    p = defaultdict(float)
    for i in nums:
        p[('単勝', (i,))] = w[i]
        for j in nums:
            if i == j:
                continue
            p2 = w[i] * w[j] / (1 - w[i])
            p[('馬単', (i, j))] = p2
            p[('馬連', tuple(sorted((i, j))))] += p2
            p[('2着内', (i,))] += p2
            p[('2着内', (j,))] += p2
            for k in nums:
                if k in (i, j):
                    continue
                denom = sum(w[n] for n in nums if n not in (i, j))
                p3 = p2 * w[k] / denom
                p[('3連単', (i, j, k))] = p3
                p[('3連複', tuple(sorted((i, j, k))))] += p3
                for n in (i, j, k):
                    p[('3着内', (n,))] += p3
                for a in itertools.combinations(sorted((i, j, k)), 2):
                    p[('ワイド', a)] += p3
    for kind, expected in [('単勝', 1), ('馬連', 1), ('馬単', 1),
                           ('3連複', 1), ('3連単', 1), ('2着内', 2), ('3着内', 3), ('ワイド', 3)]:
        actual = sum(v for (t, _), v in p.items() if t == kind)
        if not math.isclose(actual, expected, abs_tol=1e-9):
            raise ValueError(f'{kind} probability checksum failed: {actual}')
    # 3着内 means top-three membership, not always the sold place-ticket event.
    # Place payout rules and frame mappings must be handled by the existing pipeline.
    return p


def export_combinations(day_json, output_path, tau=1.0):
    now = datetime.now(JST)
    rows, errors = [], []
    seen = set()
    for race in day_json.get('races', []):
        rid = str(race.get('race_id', '')).replace('\\_', '_')
        try:
            if not rid or rid in seen:
                raise ValueError('missing/duplicate race_id')
            seen.add(rid)
            probabilities = ticket_probabilities(race, tau)
            post_text = race['post_time']
            if len(post_text) == 5 and post_text[2] == ':':
                post = datetime.fromisoformat(f"{race['race_date']}T{post_text}:00+09:00")
            else:
                post = datetime.fromisoformat(post_text)
            if post.tzinfo is None:
                raise ValueError('post_time requires timezone')
            race_status = 'unstarted' if now < post else 'started_or_finished'
            api = {int(h['horse_no']): h.get('pl_place_prob') for h in race['horses']}
            for (kind, selection), probability in sorted(probabilities.items()):
                rows.append(dict(zip(FIELDS, [
                    rid, kind, '-'.join(map(str, selection)), probability,
                    probability * 100, 1 / probability,
                    api.get(selection[0], '') if kind == '3着内' else '',
                    tau, 'PL_exact_top3_v1', 'unverified',
                    race.get('model_provenance', {}).get('model_id', day_json.get('model_id', '')),
                    json.dumps(race.get('model_provenance', {}), ensure_ascii=False, sort_keys=True),
                    day_json.get('generated_at', ''), now.isoformat(), post.isoformat(),
                    race_status, race.get('odds_snapshot_at', ''), '', '',
                    'reference_estimate_price_missing' if race_status == 'unstarted'
                    else 'not_for_pre_race_use',
                ])))
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            errors.append({'race_id': rid, 'error': str(exc)})
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {'rows': len(rows), 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='already-fetched Euphoria day JSON')
    parser.add_argument('--output', required=True)
    parser.add_argument('--tau', type=float, default=1.0)
    args = parser.parse_args()
    with open(args.input, encoding='utf-8-sig') as f:
        day = json.load(f)
    result = export_combinations(day, args.output, args.tau)
    print(json.dumps(result, ensure_ascii=False))
    if result['errors']:
        raise SystemExit(1)
