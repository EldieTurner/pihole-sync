"""Publish the checked-in Docker Hub overview with a dedicated API token."""
from pathlib import Path
import json
import os
import urllib.error
import urllib.request


def request(path, body, token=None, method='POST'):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(
        'https://hub.docker.com' + path, data=json.dumps(body).encode(),
        headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    username = os.environ['DOCKERHUB_USERNAME']
    secret = os.environ['DOCKERHUB_TOKEN']
    repository = os.environ.get('DOCKERHUB_REPOSITORY', 'pihole-sync')
    root = Path(__file__).resolve().parents[1]
    overview = (root / 'docs' / 'DOCKERHUB.md').read_text(encoding='utf-8')
    try:
        auth = request('/v2/auth/token', {'identifier': username, 'secret': secret})
        bearer = auth.get('access_token') or auth['token']
        request('/v2/repositories/' + username + '/' + repository + '/', {
            'description': 'Sync Pi-hole v6 lists, rules and settings from a primary to replicas, with a dashboard.',
            'full_description': overview,
        }, token=bearer, method='PATCH')
    except urllib.error.HTTPError as error:
        raise SystemExit('Docker Hub overview update failed; HTTP ' + str(error.code)) from None
    print('Updated Docker Hub overview: ' + username + '/' + repository)


if __name__ == '__main__':
    main()
