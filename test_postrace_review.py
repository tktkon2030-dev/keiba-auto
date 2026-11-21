import copy
import json
import math
import unittest
from datetime import datetime, timedelta

from keiba_scraper import JST
from postrace_results import parse_result, selection
from postrace_review import (capture, digest, evaluate, latest_snapshots,
                             propose_temperature, race_observation, score,
                             settle, summarize_settlements)

RID = '2026_1115_08_05_04_11'


def horse_rows(captured='2026-11-15T10:00:00+09:00'):
    return [{'元レースID': RID, '日付': '2026-11-15', '取得日時': captured,
             '発走予定時刻': '15:40', 'API生成日時': '2026-11-15T09:55:00+09:00',
             '事前予想有効': '1', '取消・除外': '0', '馬番': str(n),
             'AI勝率(%)': str(p), 'モデルID': 'model-a', 'モデル情報': '{"weights":"abc"}',
             'モデル更新日時': captured, '開催場名': '京都', '馬場状態': '重'}
            for n, p in enumerate([40, 30, 20, 10], 1)]


def candidate(kind='単勝', combo='1', quoted=4, status='購入検討'):
    return {'元レースID': RID, '取得日時': '2026-11-15T10:00:00+09:00', '券種': kind,
            '組み合わせ': combo, '推定的中確率(%)': '40', '提示オッズ': str(quoted), '判定': status}


def result():
    return {'race_id': RID, 'finish': {1: 1, 2: 2, 3: 3, 4: 4}, 'scratched': [],
            'payouts': {'単勝': {'1': 400}, '複勝': {'1': 150, '2': 200, '3': 300},
                        'ワイド': {'1-2': 500, '1-3': 700, '2-3': 1000},
                        '枠連': {'1-1': 400}, '馬連': {'1-2': 2000},
                        '馬単': {'1-2': 3000}, '3連複': {'1-2-3': 4000},
                        '3連単': {'1-2-3': 9000}},
            'fetched_at': '2026-11-15T18:30:00+09:00'}


def parsed_fixture(tie=False):
    rows = ''.join(f'<tr class="HorseList"><td><div class="Rank">{rank}</div></td>'
                   f'<td class="Num Waku1">1</td><td class="Num Txt_C">{no}</td></tr>'
                   for no, rank in [(1, 1), (2, 1 if tie else 2), (3, 3), (4, 4)])
    payouts = ('<tr class="Tansho"><td class="Result"><div><span>1</span></div>'
               '<div><span></span></div>' + ('<div><span>2</span></div>' if tie else '') +
               '</td><td class="Payout"><span>400円' + ('<br>300円' if tie else '') + '</span></td></tr>')
    payouts += ('<tr class="Wide"><td class="Result"><ul><li><span>2</span></li>'
                '<li><span>1</span></li><li></li></ul></td>'
                '<td class="Payout"><span>1,230円</span></td></tr>')
    return ('<link rel="canonical" href="https://race.netkeiba.com/race/result.html?race_id=202608050411">'
            '<div class="RaceData02">4頭</div><table id="All_Result_Table">' + rows +
            '</table><table class="Payout_Detail_Table">' + payouts + '</table>')


class SnapshotTests(unittest.TestCase):
    def test_full_pre_race_snapshot_and_hash(self):
        bundle = capture(horse_rows(), [candidate()])
        race = bundle['races'][0]
        self.assertEqual(len(race['horses']), 4)
        self.assertEqual(len(race['candidates']), 1)
        self.assertEqual(latest_snapshots([bundle])[RID]['snapshot_id'], race['snapshot_id'])
        race['horses'][0]['AI勝率(%)'] = '90'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            latest_snapshots([bundle])

    def test_finished_future_source_and_invalid_mass_rejected(self):
        for mode in ('finished', 'future', 'mass', 'mixed', 'invalid', 'missing_source'):
            rows = horse_rows()
            if mode == 'finished':
                for row in rows:
                    row['取得日時'] = '2026-11-15T15:40:00+09:00'
            elif mode == 'future':
                for row in rows:
                    row['API生成日時'] = '2026-11-15T11:00:00+09:00'
            elif mode == 'mass': rows[0]['AI勝率(%)'] = '80'
            elif mode == 'mixed': rows[0]['モデルID'] = 'model-b'
            elif mode == 'invalid': rows[0]['事前予想有効'] = '0'
            else:
                for row in rows: row['API生成日時'] = ''
            with self.subTest(mode=mode):
                self.assertFalse(capture(rows, [])['races'])

    def test_cancelled_probability_does_not_corrupt_active_mass(self):
        rows = horse_rows()
        extra = dict(rows[0], **{'馬番': '5', '取消・除外': '1', 'AI勝率(%)': '29.05'})
        self.assertEqual(len(capture(rows + [extra], [])['races'][0]['horses']), 4)

    def test_latest_whole_snapshot_no_horse_mixing_or_duplicate_samples(self):
        first = capture(horse_rows(), [])
        second = capture(horse_rows('2026-11-15T11:00:00+09:00'), [])
        selected = latest_snapshots([first, second, first])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[RID]['captured_at'], '2026-11-15T11:00:00+09:00')
        self.assertEqual(first['races'][0]['model_key'], second['races'][0]['model_key'])


class ResultTests(unittest.TestCase):
    def test_html_parser_ignores_empty_placeholders_and_reads_yen(self):
        r = parse_result(parsed_fixture(), RID, datetime.now(JST))
        self.assertEqual(r['payouts']['単勝'], {'1': 400})
        self.assertEqual(r['payouts']['ワイド'], {'1-2': 1230})
        self.assertFalse(r['official_verified'])

    def test_multiple_winners_preserved_not_invented_order(self):
        r = parse_result(parsed_fixture(tie=True), RID, datetime.now(JST))
        self.assertEqual(set(r['payouts']['単勝']), {'1', '2'})
        with self.assertRaisesRegex(ValueError, 'dead heat'):
            race_observation(capture(horse_rows(), [])['races'][0], r)

    def test_bad_identity_incomplete_rows_and_payout_mismatch_fail(self):
        for page in [parsed_fixture().replace('202608050411', '202608050412'),
                     parsed_fixture().replace('4頭', '18頭'),
                     parsed_fixture().replace('400円', '計算中'),
                     parsed_fixture().replace('class="Rank">4', 'class="Rank">未確定')]:
            with self.assertRaises(ValueError):
                parse_result(page, RID, datetime.now(JST))

    def test_ordered_and_unordered_selections(self):
        self.assertEqual(selection('3連複', '3-1-2'), '1-2-3')
        self.assertEqual(selection('3連単', '3-1-2'), '3-1-2')
        self.assertEqual(selection('枠連', '1-1'), '1-1')
        with self.assertRaises(ValueError): selection('馬連', '1-1')


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.race = capture(horse_rows(), [candidate(), candidate('ワイド', '1-2', '', '価格待ち'),
                                          candidate('単勝', '2', 99, '見送り')])['races'][0]

    def test_missing_results_are_not_losses(self):
        review = evaluate({RID: self.race}, {})
        self.assertEqual(review['scores']['races'], 0)
        self.assertIsNone(review['simulation']['単勝']['roi'])
        self.assertIsNone(review['actual'])

    def test_ex_ante_simulation_separate_from_actual_and_binary_scoring(self):
        r = evaluate({RID: self.race}, {RID: result()})
        self.assertEqual(r['simulation']['単勝']['stake_yen'], 100)
        self.assertEqual(r['simulation']['単勝']['payout_yen'], 400)
        self.assertIsNone(r['actual'])
        self.assertEqual(r['ticket_probability_scores']['ワイド']['candidate_count'], 1)
        self.assertEqual(r['simulation']['ワイド']['tickets'], 0)
        self.assertAlmostEqual(r['scores']['log_loss'], -math.log(.4))
        self.assertAlmostEqual(r['scores']['brier'], .5)

    def test_incomplete_result_cannot_generate_false_losses(self):
        partial = result()
        partial['finish'].pop(4)
        r = evaluate({RID: self.race}, {RID: partial})
        self.assertFalse(r['simulation_rows'])
        self.assertEqual(r['scores']['races'], 0)

    def test_missing_payout_and_refund_not_loss(self):
        r = result()
        r['payouts'].pop('3連単')
        self.assertEqual(settle('3連単', '2-1-3', 200, r), ('payout_missing', None))
        r['scratched'] = [4]
        self.assertEqual(settle('馬連', '1-4', 300, r), ('refunded', 300))
        self.assertEqual(settle('枠連', '1-1', 100, r), ('refund_unverified', None))
        r['finish'][4] = '取消'
        with self.assertRaises(ValueError): race_observation(self.race, r)

    def test_actual_ledger_yen_and_purchase_time(self):
        bet = {'purchase_id': 'ipat-1', 'race_id': RID, 'bet_type': '単勝',
               'selection': '1', 'stake_yen': '300', 'purchased_at': '2026-11-15T10:05:00+09:00'}
        r = evaluate({RID: self.race}, {RID: result()}, [bet])
        self.assertEqual(r['actual']['単勝']['payout_yen'], 1200)
        for change in [{'stake_yen': '150'}, {'purchased_at': '2026-11-15T15:40:00+09:00'}]:
            with self.assertRaises(ValueError): evaluate({RID: self.race}, {RID: result()}, [dict(bet, **change)])
        with self.assertRaises(ValueError): evaluate({RID: self.race}, {RID: result()}, [bet, bet])

    def test_small_samples_never_claim_learning_complete(self):
        obs = race_observation(self.race, result())
        proposal = propose_temperature([obs] * 199)
        self.assertEqual(proposal['status'], 'insufficient_history')
        self.assertFalse(proposal['auto_applied'])

    def test_training_split_is_by_date_and_improvement_is_not_auto_applied(self):
        observations = []
        start = datetime(2026, 1, 1)
        for day in range(10):
            for n in range(30):
                observations.append({'race_id': f'{day}-{n}', 'date': (start + timedelta(days=day)).date().isoformat(),
                                     'p': {1: .95, 2: .05}, 'winner': 1 if n % 2 else 2})
        p = propose_temperature(observations)
        self.assertEqual(p['train_races'], 240)
        self.assertEqual(p['holdout_races'], 60)
        self.assertLess(p['trained_through'], p['holdout_start_date'])
        self.assertEqual(p['status'], 'review_candidate')
        self.assertGreater(p['temperature'], 1)
        self.assertFalse(p['auto_applied'])


if __name__ == '__main__':
    unittest.main()
