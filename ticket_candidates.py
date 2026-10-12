"""Create a shortlist with actual-price and proxy-price EV kept separate."""

import csv
import io
import math
from collections import defaultdict
from datetime import datetime

from euphoria_combinations import ticket_probabilities

SOLD_TYPES = ('単勝', '複勝', '枠連', '馬連', 'ワイド', '馬単', '3連複', '3連単')
MIN_PROBABILITY = {
    '単勝': 0.03, '複勝': 0.12, '枠連': 0.04, '馬連': 0.02,
    'ワイド': 0.05, '馬単': 0.01, '3連複': 0.005, '3連単': 0.002,
}
FIELDS = (
    '取得日時', '日付', '元レースID', '開催場名', 'R', 'レース名', '発走予定時刻',
    '券種', '組み合わせ', '券種内順位', '推定的中確率(%)', '損益分岐オッズ',
    '最低購入オッズ', '提示オッズ', 'オッズ基準日時', '価格経過分', 'EV倍率',
    '参考推定オッズ', '推定EV倍率', '半値想定EV倍率',
    '推定元馬連オッズ', '推定元オッズ基準日時',
    '判定', '判定理由', 'モデル確率検証',
)


def _positive_number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError):
        return None


def _age_minutes(value, captured):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None or captured.tzinfo is None:
            return None
        age = (captured - dt).total_seconds() / 60
        return round(age, 1) if age >= 0 else None
    except ValueError:
        return None


def _market_probabilities_by_race(horses):
    """PL market shape from normalized inverse win prices; no result is a quote."""
    grouped = defaultdict(list)
    for horse in horses:
        if str(horse.get('取消・除外', '0')) not in ('0', '', 'False'):
            continue
        grouped[horse['元レースID']].append(horse)
    result = {}
    for race_id, active in grouped.items():
        weights = []
        for horse in active:
            odds = _positive_number(horse.get('単勝オッズ'))
            try:
                number = int(horse['馬番'])
            except (KeyError, TypeError, ValueError):
                odds = None
            if odds is None:
                break
            weights.append({'horse_no': number, 'model_share': 1 / odds})
        else:
            if len(weights) >= 3:
                try:
                    result[race_id] = ticket_probabilities({'horses': weights})
                except ValueError:
                    pass
    return result


def build_candidate_rows(combinations, horses, captured, *, min_ev=1.20,
                         max_price_age_minutes=90, limit_per_type=5):
    """Return up to limit_per_type rows per race and sold ticket type.

    Prices already include the pari-mutuel deduction; min_ev is applied to
    probability * displayed odds without a second deduction. The shortlist is
    for human review, never an automated stake or a guarantee of profit.
    """
    if not (math.isfinite(min_ev) and min_ev > 1):
        raise ValueError('min_ev must be finite and greater than one')
    if limit_per_type < 1 or max_price_age_minutes <= 0:
        raise ValueError('invalid shortlist limit or price age')
    metadata = {}
    for horse in horses:
        metadata.setdefault(horse['元レースID'], horse)
    market_probabilities = _market_probabilities_by_race(horses)
    quinella_quotes = {}
    for item in combinations:
        if item['券種'] == '馬連':
            odds = _positive_number(item.get('提示オッズ'))
            if odds is not None:
                quinella_quotes[item['元レースID'], item['組み合わせ']] = (
                    odds, item.get('オッズ基準日時', ''))
    grouped = defaultdict(list)
    for item in combinations:
        kind = item['券種']
        if kind not in SOLD_TYPES:
            continue  # 2着内 and 3着内 are diagnostic probabilities, not tickets.
        probability = _positive_number(item.get('推定的中確率'))
        if probability is None or probability > 1:
            continue
        race_id = item['元レースID']
        meta = metadata.get(race_id, {})
        quoted = _positive_number(item.get('提示オッズ'))
        age = _age_minutes(item.get('オッズ基準日時'), captured) if quoted else None
        threshold = math.ceil(min_ev / probability * 10 - 1e-10) / 10
        eligible = str(item.get('事前予想有効')) == '1'
        p_ok = probability >= MIN_PROBABILITY[kind]
        ev = probability * quoted if quoted is not None and age is not None else None
        reference, anchor_odds, anchor_clock = '', '', ''
        if quoted is None and kind in ('馬単', 'ワイド'):
            try:
                a, b = map(int, item['組み合わせ'].split('-'))
                pair = '-'.join(map(str, sorted((a, b))))
                anchor = quinella_quotes.get((race_id, pair))
                market = market_probabilities.get(race_id, {})
                q_pair = market.get(('馬連', (min(a, b), max(a, b))))
                q_ticket = market.get((kind, (a, b) if kind == '馬単'
                                       else (min(a, b), max(a, b))))
                if anchor and q_pair and q_ticket:
                    payout_ratio = .75 / .775 if kind == '馬単' else 1.0
                    reference = round(anchor[0] * payout_ratio * q_pair / q_ticket, 2)
                    anchor_odds, anchor_clock = anchor
            except ValueError:
                pass
        estimated_ev = probability * reference if reference else None
        stressed_ev = estimated_ev * .5 if estimated_ev is not None else None
        anchor_age = _age_minutes(anchor_clock, captured) if anchor_clock else None
        if not eligible:
            status, reason = '事前評価不可', '発走済み・時刻不正など'
        elif not p_ok:
            status, reason = '参考', '的中確率が券種別の候補基準未満'
        elif quoted is None and stressed_ev is not None and stressed_ev >= min_ev:
            status = '推定EV候補' if anchor_age is not None and anchor_age <= max_price_age_minutes else '推定EV候補・価格再確認'
            reason = '馬連起点の推定オッズが半値でもEV基準を満たす（実価格未取得）'
        elif quoted is None:
            status, reason = '価格待ち', '最低購入オッズに達する実価格を確認する'
        elif age is None:
            status, reason = '価格再確認', 'オッズ基準日時が不明・不正・未来'
        elif age > max_price_age_minutes:
            status, reason = '価格再確認', 'オッズが古いため購入判断を保留'
        elif ev >= min_ev:
            status, reason = '購入検討', '表示オッズでEV基準を満たす（モデル未検証）'
        else:
            status, reason = '見送り', '表示オッズでEV基準を満たさない'
        row = {
            '取得日時': captured.isoformat(), '日付': item.get('日付', ''),
            '元レースID': race_id, '開催場名': meta.get('開催場名', item.get('場名', '')),
            'R': item.get('R', ''), 'レース名': meta.get('レース名', ''),
            '発走予定時刻': meta.get('発走予定時刻', ''), '券種': kind,
            '組み合わせ': item['組み合わせ'], '券種内順位': '',
            '推定的中確率(%)': round(probability * 100, 3),
            '損益分岐オッズ': round(1 / probability, 2),
            '最低購入オッズ': threshold,
            '提示オッズ': quoted if quoted is not None else '',
            'オッズ基準日時': item.get('オッズ基準日時', ''),
            '価格経過分': age if age is not None else '',
            'EV倍率': round(ev, 3) if ev is not None else '',
            '参考推定オッズ': reference,
            '推定EV倍率': round(estimated_ev, 3) if estimated_ev is not None else '',
            '半値想定EV倍率': round(stressed_ev, 3) if stressed_ev is not None else '',
            '推定元馬連オッズ': anchor_odds,
            '推定元オッズ基準日時': anchor_clock,
            '判定': status, '判定理由': reason,
            'モデル確率検証': item.get('確率検証', '未検証'),
        }
        # Rank actual-price candidates first, then proxy-price candidates.
        if p_ok:
            grouped[race_id, kind].append((probability, ev, row))

    out = []
    for key in sorted(grouped, key=lambda x: (x[0], SOLD_TYPES.index(x[1]))):
        group = grouped[key]
        group.sort(key=lambda entry: (
            0 if entry[2]['判定'] == '購入検討' else
            1 if entry[2]['判定'] == '推定EV候補' else
            2 if entry[2]['判定'] == '推定EV候補・価格再確認' else
            3 if entry[2]['判定'] == '価格再確認' else
            4 if entry[2]['判定'] == '価格待ち' else 5,
            -(entry[1] if entry[1] is not None else
              entry[2]['半値想定EV倍率'] if entry[2]['半値想定EV倍率'] != ''
              else entry[0]),
            entry[2]['組み合わせ'],
        ))
        for rank, (_, _, row) in enumerate(group[:limit_per_type], 1):
            row['券種内順位'] = rank
            out.append(row)
    return out


def candidate_csv(rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
