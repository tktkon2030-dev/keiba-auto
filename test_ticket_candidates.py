import unittest
from datetime import datetime, timedelta, timezone

from ticket_candidates import build_candidate_rows, candidate_csv

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=JST)


def ticket(kind, selection, probability, price='', snapshot='', valid=1,
           clock_source='API明示時刻'):
    return {'券種': kind, '組み合わせ': selection,
            '推定的中確率': probability, '提示オッズ': price,
            'オッズ基準日時': snapshot, 'オッズ時刻採用方法': clock_source,
            '事前予想有効': valid,
            '元レースID': 'race-1', '日付': '2026-10-12', 'R': 1,
            '確率検証': '未検証'}


HORSES = [{'元レースID': 'race-1', '開催場名': '京都', 'レース名': '確認用',
           '発走予定時刻': '10:30'}]


class TicketCandidatesTest(unittest.TestCase):
    def test_missing_odds_gives_threshold_without_ev_or_buy_signal(self):
        rows = build_candidate_rows([ticket('ワイド', '1-2', .2)], HORSES, NOW)
        self.assertEqual(rows[0]['最低購入オッズ'], 6.0)
        self.assertEqual(rows[0]['判定'], '価格待ち')
        self.assertEqual(rows[0]['EV倍率'], '')
        self.assertIn('ワイド', candidate_csv(rows))

    def test_fresh_and_stale_prices(self):
        fresh = (NOW - timedelta(minutes=15)).isoformat()
        stale = (NOW - timedelta(hours=12)).isoformat()
        rows = build_candidate_rows([
            ticket('馬連', '1-2', .1, 15, fresh),
            ticket('馬連', '1-3', .1, 30, stale),
        ], HORSES, NOW)
        self.assertEqual({r['組み合わせ']: r['判定'] for r in rows},
                         {'1-2': '購入検討', '1-3': '価格再確認'})
        self.assertEqual(rows[0]['EV倍率'], 1.5)
        self.assertEqual(rows[0]['最低購入オッズ'], 12.0)

    def test_unsold_probabilities_and_low_probability_are_not_promoted(self):
        rows = build_candidate_rows([
            ticket('3着内', '1', .4),
            ticket('3連単', '1-2-3', .0001, 99999, NOW.isoformat()),
            ticket('単勝', '1', .1, 20, NOW.isoformat(), valid=0),
        ], HORSES, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['判定'], '事前評価不可')

    def test_shortlist_limits_each_type_and_keeps_strongest_prices(self):
        combinations = [ticket('単勝', str(n), .1, n + 10, NOW.isoformat())
                        for n in range(1, 8)]
        rows = build_candidate_rows(combinations, HORSES, NOW, limit_per_type=2)
        self.assertEqual([row['組み合わせ'] for row in rows], ['7', '6'])

    def test_quinella_proxy_is_separate_from_actual_ev(self):
        horses = [{'元レースID': 'race-1', '馬番': n,
                   '単勝オッズ': odds, '取消・除外': 0,
                   'オッズ時刻採用方法': 'API明示時刻',
                   'オッズ基準日時': NOW.isoformat()}
                  for n, odds in [(1, 2.5), (2, 4.0), (3, 8.0), (4, 12.0)]]
        rows = build_candidate_rows([
            ticket('馬連', '1-2', .2, 10, NOW.isoformat()),
            ticket('馬単', '1-2', .12),
            ticket('ワイド', '1-2', .35),
        ], horses, NOW)
        results = {r['券種']: r for r in rows}
        for kind in ('馬単', 'ワイド'):
            self.assertGreater(results[kind]['参考推定オッズ'], 0)
            self.assertEqual(results[kind]['推定元馬連オッズ'], 10)
            self.assertEqual(results[kind]['判定'], '価格待ち')
            self.assertEqual(results[kind]['EV倍率'], '')
            self.assertGreater(results[kind]['推定EV倍率'], 0)
        self.assertGreater(results['馬単']['参考推定オッズ'], 10)
        self.assertLess(results['ワイド']['参考推定オッズ'], 10)

    def test_proxy_ev_promotes_only_under_stress_and_marks_stale_anchor(self):
        horses = [{'元レースID': 'race-1', '馬番': n,
                   '単勝オッズ': odds, '取消・除外': 0,
                   'オッズ時刻採用方法': 'API明示時刻',
                   'オッズ基準日時': NOW.isoformat()}
                  for n, odds in [(1, 2.5), (2, 4), (3, 8), (4, 12)]]
        for anchor_clock, expected in [
            (NOW.isoformat(), '推定EV候補'),
            ((NOW - timedelta(hours=12)).isoformat(), '推定EV候補・価格再確認'),
        ]:
            rows = build_candidate_rows([
                ticket('馬連', '1-2', .5, 30, anchor_clock),
                ticket('馬単', '1-2', .3),
            ], horses, NOW)
            exacta = next(r for r in rows if r['券種'] == '馬単')
            self.assertEqual(exacta['判定'], expected)
            self.assertGreaterEqual(exacta['半値想定EV倍率'], 1.2)
            self.assertEqual(exacta['EV倍率'], '')

    def test_save_time_substitution_never_becomes_fresh_buy_signal(self):
        row = build_candidate_rows([
            ticket('単勝', '1', .2, 10, NOW.isoformat(),
                   clock_source='保存時刻で代用')], HORSES, NOW)[0]
        self.assertEqual(row['判定'], '価格再確認')
        horses = [{'元レースID': 'race-1', '馬番': n,
                   '単勝オッズ': odds, '取消・除外': 0,
                   'オッズ時刻採用方法': '保存時刻で代用',
                   'オッズ基準日時': NOW.isoformat()}
                  for n, odds in [(1, 2.5), (2, 4), (3, 8), (4, 12)]]
        rows = build_candidate_rows([
            ticket('馬連', '1-2', .5, 30, NOW.isoformat()),
            ticket('馬単', '1-2', .3),
        ], horses, NOW)
        exacta = next(r for r in rows if r['券種'] == '馬単')
        self.assertEqual(exacta['判定'], '推定EV候補・価格再確認')


if __name__ == '__main__':
    unittest.main()
