"""Select one complete, explicit probability vector; never guess new API keys."""
from copy import deepcopy
import math


def at(obj, path):
    if not isinstance(path, list) or not path or not all(isinstance(k, str) for k in path):
        raise ValueError('model paths must be non-empty arrays of keys')
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def select_day(raw, profile=None):
    """Profile paths are operator supplied after checking an actual API response."""
    day = deepcopy(raw)
    if profile is not None:
        for key in ('horse_probability_path', 'race_model_id_path'):
            at({}, profile.get(key))
        expected = profile.get('expected_model_id')
        if not isinstance(expected, str) or not expected:
            raise ValueError('expected_model_id required')
        if profile.get('unit') not in ('fraction', 'percent'):
            raise ValueError('profile unit must be fraction or percent')
    for race in day.get('races', []):
        provenance = race.get('model_provenance') or {}
        model_id = provenance.get('model_id') or day.get('model_id') or 'unknown'
        path = ['model_share']
        if profile:
            model_id = at(race, profile['race_model_id_path'])
            if model_id != profile['expected_model_id']:
                raise ValueError('selected model ID missing or unexpected; no fallback')
            path = profile['horse_probability_path']
        horses = race.get('horses') or []
        active = [h for h in horses if h.get('scratched') is not True]
        if len(active) < 3:
            raise ValueError('at least three active horses required')
        ids = [int(h['horse_no']) for h in horses]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate horse_no')
        values = []
        for h in active:
            v = at(h, path)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ValueError('selected model incomplete/invalid; no per-horse fallback')
            v = v / 100 if profile and profile['unit'] == 'percent' else v
            if v > 1:
                raise ValueError('selected probability outside (0,1]')
            values.append(v)
        total = sum(values)
        # Permit cancelled runners' mass to be redistributed, not arbitrary weights.
        if total > 1.02 or (not any(h.get('scratched') is True for h in horses) and total < .98):
            raise ValueError('selected probability mass inconsistent with declared unit')
        ranked = sorted(zip(active, values), key=lambda hv: hv[1], reverse=True)
        for rank, (h, value) in enumerate(ranked, 1):
            h['legacy_model_share'] = h.get('model_share')
            h['model_share'] = value / total
            h['strength_rank'] = rank
            market = h.get('market_share')
            h['divergence'] = h['model_share'] - market if isinstance(market, (float, int)) and not isinstance(market, bool) else None
            h['pl_place_prob'] = None  # Recomputed from this vector, not another model's marginal.
        for h in horses:
            if h.get('scratched') is True:
                h['legacy_model_share'] = h.get('model_share')
                h['model_share'] = None
                h['pl_place_prob'] = None
                h['strength_rank'] = None
        race['model_provenance'] = ({'model_id': model_id, 'selection_source_path': '.'.join(path), 'validation': 'unverified'} if profile else dict(provenance, model_id=model_id))
        race['selected_model_updated_at'] = at(race, profile['race_model_updated_at_path']) if profile and profile.get('race_model_updated_at_path') else None
        race['selected_model_path'] = '.'.join(path)
        race['selected_model_policy'] = 'explicit_profile' if profile else 'legacy_explicit (site alignment unverified)'
        race['selected_probability_mass'] = total
    return day


def schema_report(raw):
    """No raw API dump: paths/types plus numeric examples, safe metadata only."""
    paths = {}
    def walk(value, path, depth=0):
        if depth > 12 or any(word in path.lower() for word in ('token', 'secret', 'cookie', 'password', 'authorization', 'webhook')):
            return
        item = paths.setdefault(path, {'types': set()})
        item['types'].add(type(value).__name__)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            item.setdefault('numeric_example', value)
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + '.' + str(key), depth + 1)
        elif isinstance(value, list):
            for child in (value if path == '$.races' else value[:3]):
                walk(child, path + '[]', depth + 1)
    walk(raw, '$')
    return {'generated_at': raw.get('generated_at'), 'model_id': raw.get('model_id'),
            'race_count': len(raw.get('races') or []),
            'paths': {p: dict(v, types=sorted(v['types'])) for p, v in sorted(paths.items())}}
