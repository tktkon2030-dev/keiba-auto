import json
import tempfile
import unittest
from pathlib import Path

from postrace_drive import DriveHistory, sha, upload
from postrace_review import canonical, load_history
from test_postrace_review import horse_rows
from postrace_review import capture


URL = 'https://script.google.com/macros/s/example-deployment/exec'


class FakeArchive:
    def __init__(self):
        self.files, self.requests = {}, []

    def __call__(self, p):
        self.requests.append(p)
        action = p['action']
        reply = {'success': True, 'archive_protocol': 1, 'action': action}
        if action == 'history_put':
            month, name, content = p['month'], p['archive_filename'], p['archive_content']
            self.files[month, name] = content
            reply.update(filename=name, sha256=sha(content))
        elif action == 'history_months':
            reply['months'] = sorted({m for m, n in self.files})
        elif action == 'history_list':
            names = sorted(n for m, n in self.files if m == p['month'])
            start = int(p.get('cursor') or 0)
            reply['files'] = [{'id': n, 'filename': n} for n in names[start:start+1]]
            reply['next_cursor'] = str(start+1) if start+1 < len(names) else ''
        elif action == 'history_get':
            content = self.files[p['month'], p['file_id']]
            reply.update(filename=p['file_id'], content=content, sha256=sha(content))
        return reply


class DriveArchiveTests(unittest.TestCase):
    def test_old_receiver_success_is_not_archive_success_and_no_csv_keys(self):
        seen = []
        def old(p):
            seen.append(p)
            return {'status': 'success', 'saved_count': 1}
        client = DriveHistory(URL, 'private', old)
        with self.assertRaises(RuntimeError): client.put('2026-11', '{}')
        self.assertNotIn('files', seen[0])
        self.assertNotIn('filename', seen[0])

    def test_durable_round_trip_and_identical_retries_do_not_duplicate(self):
        api = FakeArchive()
        client = DriveHistory(URL, 'private', api)
        obj = capture(horse_rows(), [])
        # The restore path has no 90-day cutoff.
        content = canonical(obj) + '\n'
        client.put('2025-01', content)
        client.put('2025-01', content)
        self.assertEqual(len(api.files), 1)
        with tempfile.TemporaryDirectory() as directory:
            client.download(directory)
            races, results = load_history(directory)
            self.assertEqual(len(races), 1)
            before = len([p for p in api.requests if p['action'] == 'history_get'])
            client.download(directory)
            self.assertEqual(len([p for p in api.requests if p['action'] == 'history_get']), before)

    def test_multiple_months_and_pagination(self):
        api = FakeArchive()
        client = DriveHistory(URL, 'private', api)
        for month, value in [('2026-10', 1), ('2026-10', 2), ('2026-11', 3)]:
            client.put(month, canonical({'kind': 'keiba_results_v1', 'results': {}, 'version': value}))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(client.download(directory)['history_files'], 3)

    def test_corrupt_remote_content_rejected(self):
        api = FakeArchive()
        client = DriveHistory(URL, 'private', api)
        client.put('2026-11', canonical({'kind': 'keiba_results_v1', 'results': {}}))
        key = next(iter(api.files))
        api.files[key] += ' '
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                client.download(directory)

    def test_results_upload_only_new_versions_not_entire_old_cache(self):
        api = FakeArchive()
        client = DriveHistory(URL, 'private', api)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'results.json').write_text(json.dumps({
                'kind': 'keiba_results_v1', 'refreshed_race_ids': ['new'],
                'results': {'old': {'fetched_at': '2026-10-01T18:00:00+09:00'},
                            'new': {'fetched_at': '2026-11-15T18:00:00+09:00'}}}))
            upload(directory, client)
        archived = json.loads(next(iter(api.files.values())))
        self.assertEqual(set(archived['results']), {'new'})

    def test_missing_token_or_invalid_endpoint_fails_without_network(self):
        for url, token in [(URL, ''), ('https://example.org', 'private')]:
            with self.assertRaises(ValueError): DriveHistory(url, token)


if __name__ == '__main__':
    unittest.main()
