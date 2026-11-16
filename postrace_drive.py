"""Durable, content-addressed history through an authenticated GAS POST API."""
import argparse
import hashlib
import json
import os
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

from postrace_review import canonical


def sha(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class DriveHistory:
    def __init__(self, url, token, transport=None):
        if not url or not token:
            raise ValueError('GAS_WEBHOOK_URL and GAS_ARCHIVE_TOKEN are required')
        if not re.fullmatch(r'https://script\.google\.com/macros/s/[\w-]+/exec', url):
            raise ValueError('invalid GAS deployment URL')
        self.url, self.token, self.transport = url, token, transport

    def request(self, action, **params):
        payload = {'action': action, 'archive_token': self.token, **params}
        try:
            if self.transport:
                reply = self.transport(payload)
            else:
                request = urllib.request.Request(self.url, data=canonical(payload).encode(),
                                                 headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    reply = json.loads(response.read().decode('utf-8'))
            if (reply.get('archive_protocol') != 1 or reply.get('success') is not True or
                    reply.get('action') != action):
                raise ValueError('archive protocol not available')
            return reply
        except Exception:
            # Exception URLs and payloads may contain deployment details/tokens.
            raise RuntimeError('Drive history request failed; check GAS deployment/token') from None

    def put(self, month, content):
        checksum = sha(content)
        name = f'history-{checksum}.json'
        reply = self.request('history_put', month=month, archive_filename=name,
                             archive_content=content, archive_sha256=checksum)
        if reply.get('sha256') != checksum or reply.get('filename') != name:
            raise ValueError('archive save acknowledgement mismatch')
        return reply

    def download(self, directory):
        months = self.request('history_months').get('months')
        if not isinstance(months, list) or any(not re.fullmatch(r'\d{4}-\d{2}', m) for m in months):
            raise ValueError('invalid archive month listing')
        count = 0
        for month in sorted(set(months)):
            cursor, seen = '', set()
            while True:
                page = self.request('history_list', month=month, cursor=cursor)
                for item in page['files']:
                    name = item['filename']
                    if not re.fullmatch(r'history-[0-9a-f]{64}\.json', name):
                        raise ValueError('invalid archive filename')
                    expected = name[8:-5]
                    path = Path(directory) / month / name
                    if path.exists() and sha(path.read_text(encoding='utf-8')) == expected:
                        count += 1
                        continue
                    file = self.request('history_get', month=month, file_id=item['id'])
                    content = file['content']
                    if file.get('filename') != name or file.get('sha256') != expected or sha(content) != expected:
                        raise ValueError('archive download checksum mismatch')
                    obj = json.loads(content)
                    if obj.get('kind') not in ('keiba_pre_race_snapshots_v1', 'keiba_results_v1'):
                        raise ValueError('unsupported archive content')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding='utf-8')
                    count += 1
                cursor = page.get('next_cursor') or ''
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError('archive cursor repeated')
                seen.add(cursor)
        return {'history_files': count, 'months': len(months), 'source': 'Google Drive long-term archive'}


def upload(directory, client):
    count = 0
    for path in sorted(Path(directory).glob('*.json')):
        obj = json.loads(path.read_text(encoding='utf-8'))
        groups = defaultdict(dict)
        if obj.get('kind') == 'keiba_pre_race_snapshots_v1':
            for race in obj['races']:
                month = race['date'][:7]
                part = groups.setdefault(month, {'kind': obj['kind'], 'races': [], 'rejected': []})
                part['races'].append(race)
        elif obj.get('kind') == 'keiba_results_v1':
            # The review cache contains older days too; save only newly retrieved
            # result versions. Old result versions are already durable on Drive.
            for rid, result in obj['results'].items():
                if rid not in obj.get('refreshed_race_ids', list(obj['results'])):
                    continue
                month = result['fetched_at'][:7]
                part = groups.setdefault(month, {'kind': obj['kind'], 'results': {}})
                part['results'][rid] = result
        for month, payload in groups.items():
            if not re.fullmatch(r'\d{4}-\d{2}', month):
                raise ValueError('invalid archive month')
            client.put(month, canonical(payload) + '\n')
            count += 1
    return {'saved_history_files': count}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('upload', 'download'))
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    client = DriveHistory(os.environ.get('GAS_WEBHOOK_URL'), os.environ.get('GAS_ARCHIVE_TOKEN'))
    print(json.dumps(upload(args.directory, client) if args.command == 'upload'
                     else client.download(args.directory)))
