import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from model_snapshot import select_day, schema_report
from keiba_scraper import build_files, main

PROFILE = {'horse_probability_path': ['test_new', 'p'],
           'race_model_id_path': ['test_model'], 'expected_model_id': 'IM1', 'unit': 'fraction'}
NOW = datetime(2026, 10, 31, 23, 30, tzinfo=timezone(timedelta(hours=9)))


def fixture():
    # Synthetic schema, NOT an assertion about Euphoria's real IM1 field names.
    return {'generated_at': '2026-10-31T23:01:00+09:00', 'model_id': 'PL-clock-v1.1',
            'races': [{'race_id': '2026_1101_05_04_11_01', 'race_date': '2026-11-01',
                       'venue': 5, 'post_time': '10:00', 'test_model': 'IM1',
                       'model_provenance': {'model_id': 'PL-clock-v1.1', 'weights_sha256': 'old'},
                       'horses': [{'horse_no': n, 'name': f'horse{n}', 'model_share': old,
                                   'test_new': {'p': new}, 'win_odds_t10': 10,
                                   'pl_place_prob': .99, 'market_share': .1}
                                  for n, old, new in [(1, .6, .2), (2, .3, .5), (3, .1, .3)]]}]}


class ModelTests(unittest.TestCase):
    def test_complete_vector_and_no_input_mutation(self):
        raw = fixture(); day = select_day(raw, PROFILE)
        self.assertEqual([h['model_share'] for h in day['races'][0]['horses']], [.2, .5, .3])
        self.assertEqual(raw['races'][0]['horses'][0]['model_share'], .6)
        self.assertNotIn('weights_sha256', day['races'][0]['model_provenance'])

    def test_missing_one_new_value_never_uses_legacy(self):
        raw = fixture(); del raw['races'][0]['horses'][1]['test_new']
        with self.assertRaises(ValueError): select_day(raw, PROFILE)

    def test_wrong_model_id_is_rejected(self):
        raw = fixture(); raw['races'][0]['test_model'] = 'different'
        with self.assertRaises(ValueError): select_day(raw, PROFILE)

    def test_invalid_weights(self):
        for v in (True, 0, -1, float('nan'), float('inf'), '0.5'):
            raw = fixture(); raw['races'][0]['horses'][0]['test_new']['p'] = v
            with self.subTest(value=v), self.assertRaises(ValueError): select_day(raw, PROFILE)

    def test_percent_and_bad_units(self):
        raw = fixture()
        for h in raw['races'][0]['horses']: h['test_new']['p'] *= 100
        profile = dict(PROFILE, unit='percent')
        self.assertAlmostEqual(select_day(raw, profile)['races'][0]['horses'][1]['model_share'], .5)
        with self.assertRaises(ValueError): select_day(raw, PROFILE)

    def test_probability_mass_rejects_missing_or_duplicate_vectors(self):
        raw = fixture()
        for h in raw['races'][0]['horses']: h['test_new']['p'] = .01
        with self.assertRaises(ValueError): select_day(raw, PROFILE)
        raw = fixture(); raw['races'][0]['horses'][1]['horse_no'] = 1
        with self.assertRaises(ValueError): select_day(raw, PROFILE)

    def test_explicit_legacy_is_labelled(self):
        race = select_day(fixture())['races'][0]
        self.assertIn('legacy_explicit', race['selected_model_policy'])
        self.assertAlmostEqual(race['horses'][0]['model_share'], .6)

    def test_scratch_normalization(self):
        raw = fixture(); horses = raw['races'][0]['horses']
        horses.append({'horse_no': 4, 'model_share': .1, 'scratched': True})
        race = select_day(raw, PROFILE)['races'][0]
        self.assertIsNone(race['horses'][3]['model_share'])
        self.assertAlmostEqual(sum(h['model_share'] or 0 for h in race['horses']), 1)

    def test_pipeline_probabilities_metadata_and_ev_agree(self):
        files, _ = build_files(fixture(), '2026-11-01', NOW, lambda _: ('test', {}), PROFILE)
        rows = [list(csv.DictReader(io.StringIO(f['content']))) for f in files]
        self.assertEqual(rows[0][0]['モデルID'], 'IM1')
        self.assertEqual(float(rows[0][0]['AI勝率(%)']), 20)
        wins = {r['組み合わせ']: r for r in rows[2] if r['券種'] == '単勝'}
        for horse in rows[0]:
            win = wins[horse['馬番']]
            self.assertAlmostEqual(float(horse['AI勝率(%)']) / 100, float(win['推定的中確率']))
            self.assertAlmostEqual(float(horse['期待値(EV)']), float(win['EV倍率']))
        self.assertTrue(all(r['モデルID'] == 'IM1' for r in rows[2] + rows[3]))
        for kind, expected in [('馬連', 1), ('3連複', 1), ('ワイド', 3)]:
            self.assertAlmostEqual(sum(float(r['推定的中確率']) for r in rows[2] if r['券種'] == kind), expected)

    def test_late_does_not_supply_old_model_probabilities(self):
        raw = fixture(); raw['late'] = {'races': {raw['races'][0]['race_id']: [{'horse_no': 1, 'pl_place_prob': .99}]}}
        files, _ = build_files(raw, '2026-11-01', NOW, lambda _: ('', {}), PROFILE)
        self.assertNotIn('.99', files[0]['content'])

    def test_schema_no_secret_values_or_unknown_strings(self):
        report = json.dumps(schema_report({'races': [{'new': {'p': .2, 'secret': 'sensitive', 'text': 'private'}}], 'token': 'sensitive'}))
        self.assertNotIn('sensitive', report); self.assertNotIn('private', report)
        self.assertIn('numeric_example', report)

    def test_diagnostics_cannot_post_or_scrape_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('sys.argv', ['keiba_scraper.py', '2026-11-01', '--inspect-models', '--output-dir', tmp]), patch('keiba_scraper.fetch_json', return_value=fixture()), patch('keiba_scraper.fetch_netkeiba_details', side_effect=AssertionError('no card')), patch('keiba_scraper.urllib.request.urlopen', side_effect=AssertionError('no post')):
                main()
            from pathlib import Path
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['model_schema.json'])

if __name__ == '__main__': unittest.main()
