"""Download retained prediction/result artifacts via the runner's gh CLI.

No credentials in URLs/logs. Results and predictions never enter the Drive
folder. Limits bound downloads; expired artifacts are explicitly counted.
"""
import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path


def gh(path):
    response = subprocess.run(['gh', 'api', path], capture_output=True, check=False)
    if response.returncode:
        raise RuntimeError('GitHub artifact request failed')
    return response.stdout


def download(repository, output):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
        raise ValueError('invalid repository')
    repo = json.loads(gh(f'repos/{repository}'))
    artifacts = []
    # At most 500 recent artifacts; bounded archive rather than unlimited history.
    for page in range(1, 6):
        obj = json.loads(gh(f'repos/{repository}/actions/artifacts?per_page=100&page={page}'))
        batch = obj.get('artifacts', [])
        artifacts.extend(a for a in batch if a['name'].startswith(('keiba-predictions-', 'keiba-review-')))
        if len(batch) < 100:
            break
    count, expired = 0, 0
    for artifact in artifacts:
        run = artifact.get('workflow_run') or {}
        # Do not train on artifacts supplied by a fork/PR branch.
        if (run.get('head_repository_id') != repo['id'] or
                run.get('head_branch') != repo['default_branch']):
            continue
        if artifact['expired']:
            expired += 1
            continue
        if artifact['size_in_bytes'] > 25_000_000:
            raise ValueError('artifact exceeds download budget')
        content = gh(f"repos/{repository}/actions/artifacts/{int(artifact['id'])}/zip")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if name != 'results.json' and not re.fullmatch(r'snapshot-[\w.-]+\.json', name):
                    continue
                if info.file_size > 25_000_000:
                    raise ValueError('expanded artifact exceeds budget')
                folder = Path(output) / str(int(artifact['id']))
                folder.mkdir(parents=True, exist_ok=True)
                data = archive.read(info)
                json.loads(data)  # Reject non-JSON and never extract archive paths.
                (folder / name).write_bytes(data)
                count += 1
    return {'downloaded_files': count, 'expired_artifacts': expired,
            'history_scope': 'up to 500 recent GitHub artifacts; retention-limited'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--output-dir', default='history')
    args = parser.parse_args()
    print(json.dumps(download(args.repository, args.output_dir)))
