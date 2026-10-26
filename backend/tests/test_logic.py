from pathlib import Path
import os, sys, tempfile
# Keep unit tests isolated and fast; dataset seeding is covered by the manifest/data integrity check.
os.environ.setdefault('MULEGUARD_SKIP_BUNDLED_DATA', '1')
os.environ.setdefault('MULEGUARD_DB', tempfile.mktemp(prefix='muleguard-test-', suffix='.db'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import app as api

def test_health():
    c=TestClient(api.app)
    assert c.get('/api/health').status_code==200

def test_login_and_dashboard():
    c=TestClient(api.app)
    r=c.post('/api/auth/login',json={'email':'admin@muleguard.local','password':os.getenv('MULEGUARD_ADMIN_PASSWORD','ChangeMe123!')})
    assert r.status_code==200
    h={'Authorization':'Bearer '+r.json()['access_token']}
    assert c.get('/api/dashboard/summary',headers=h).status_code==200
    assert c.get('/api/dataset/info').status_code==200

def test_demo_load_and_detection():
    c=TestClient(api.app)
    r=c.post('/api/auth/login',json={'email':'admin@muleguard.local','password':os.getenv('MULEGUARD_ADMIN_PASSWORD','ChangeMe123!')})
    h={'Authorization':'Bearer '+r.json()['access_token']}
    loaded=c.post('/api/demo/load',headers=h)
    assert loaded.status_code==200
    d=c.post('/api/detection/run',headers=h)
    assert d.status_code==200 and d.json()['assessed']>=1

def test_bundled_manifest_matches_source_extract():
    import gzip, csv, json
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'data'/'manifest.json').read_text(encoding='utf-8'))
    with gzip.open(root/'data'/'soa_transactions.csv.gz','rt',encoding='utf-8',newline='') as f:
        rows=sum(1 for _ in csv.DictReader(f))
    assert rows==manifest['total_rows']==201783
