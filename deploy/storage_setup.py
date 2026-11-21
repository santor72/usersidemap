"""Generate private, bucket-scoped SeaweedFS identities from Compose environment."""
import json
import os
from pathlib import Path

bucket = os.environ['TILES_S3_BUCKET']
identities = []
for role, actions in [('WORKER', ['Read', 'List', 'Write']), ('API', ['Read'])]:
    access = os.environ[f'TILES_S3_{role}_ACCESS_KEY']
    secret = os.environ[f'TILES_S3_{role}_SECRET_KEY']
    if not access or len(secret) < 32:
        raise ValueError('Set distinct S3 keys and secrets of at least 32 characters')
    identities.append({'name': role.lower(), 'credentials': [
        {'accessKey': access, 'secretKey': secret}],
        'actions': [f'{action}:{bucket}' for action in actions]})
if identities[0]['credentials'][0]['accessKey'] == identities[1]['credentials'][0]['accessKey']:
    raise ValueError('API and worker S3 keys must differ')
path = Path('/config/s3.json')
temporary = path.with_suffix('.tmp')
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as handle:
    json.dump({'identities': identities}, handle)
    handle.flush()
    os.fsync(handle.fileno())
temporary.replace(path)
print('S3 bucket identities configured')
