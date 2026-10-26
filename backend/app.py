from __future__ import annotations
import os, csv, io, json, sqlite3, hashlib, secrets, hmac, math, gzip
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import networkx as nx

ROOT=Path(__file__).resolve().parent
DB_PATH=Path(os.getenv('MULEGUARD_DB', str(ROOT/'muleguard.db')))
app=FastAPI(title='MULEGUARD AI API', version='1.0.0', description='Explainable financial investigation prototype. Synthetic demo records are not real bank data.')
app.add_middleware(CORSMiddleware, allow_origins=os.getenv('CORS_ORIGINS','http://127.0.0.1:5500,http://localhost:5500,http://127.0.0.1:8000,http://localhost:8000').split(','), allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
SESSIONS={}

SCHEMA='''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, account_id TEXT UNIQUE NOT NULL, label TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(
 id INTEGER PRIMARY KEY, transaction_id TEXT UNIQUE NOT NULL, source_account TEXT NOT NULL,
 destination_account TEXT NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'INR',
 timestamp TEXT NOT NULL, transaction_type TEXT DEFAULT 'transfer', status TEXT DEFAULT 'recorded',
 channel TEXT DEFAULT '', source_record_id TEXT DEFAULT '', direction TEXT DEFAULT 'transfer',
 statement_owner TEXT DEFAULT '', balance REAL DEFAULT 0, counterparty_ref TEXT DEFAULT '',
 source_file TEXT DEFAULT '', source_row INTEGER DEFAULT 0, original_transaction_id TEXT DEFAULT '',
 narration_ref TEXT DEFAULT '', remarks_ref TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tx_source ON transactions(source_account); CREATE INDEX IF NOT EXISTS idx_tx_dest ON transactions(destination_account); CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions(timestamp);
CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY, complaint_id TEXT UNIQUE NOT NULL, complaint_date TEXT NOT NULL, reported_amount REAL DEFAULT 0, transaction_id TEXT DEFAULT '', account_id TEXT DEFAULT '', recipient_id TEXT DEFAULT '', status TEXT DEFAULT 'open', description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS assessments(id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, score INTEGER NOT NULL, band TEXT NOT NULL, factors TEXT NOT NULL, evidence TEXT NOT NULL, assessed_at TEXT NOT NULL, UNIQUE(account_id));
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY, case_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, summary TEXT DEFAULT '', status TEXT DEFAULT 'New', priority TEXT DEFAULT 'Medium', assigned_to TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closure_reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS case_links(id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, UNIQUE(case_id,entity_type,entity_id));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, author TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, account_id TEXT NOT NULL, score INTEGER NOT NULL, severity TEXT NOT NULL, reason TEXT NOT NULL, status TEXT DEFAULT 'New', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, actor TEXT DEFAULT 'system', action TEXT NOT NULL, entity_type TEXT DEFAULT '', entity_id TEXT DEFAULT '', details TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, report_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT DEFAULT 'system');
'''
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def connect():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c
def audit(actor, action, et='', eid='', details=''):
    with connect() as c: c.execute('INSERT INTO audit_logs(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)',(actor,action,et,eid,details[:1000],now()))
def pw_hash(password, salt=None):
    salt=salt or secrets.token_hex(16); digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1).hex(); return salt+'$'+digest
def pw_ok(password, stored):
    try: salt,digest=stored.split('$',1); return hmac.compare_digest(hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1).hex(),digest)
    except Exception: return False
def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        # Migrate databases created by earlier versions without losing existing records.
        cols={r['name'] for r in c.execute('PRAGMA table_info(transactions)').fetchall()}
        migrations={
            'direction': "TEXT DEFAULT 'transfer'", 'statement_owner': "TEXT DEFAULT ''",
            'balance': 'REAL DEFAULT 0', 'counterparty_ref': "TEXT DEFAULT ''",
            'source_file': "TEXT DEFAULT ''", 'source_row': 'INTEGER DEFAULT 0',
            'original_transaction_id': "TEXT DEFAULT ''", 'narration_ref': "TEXT DEFAULT ''",
            'remarks_ref': "TEXT DEFAULT ''"
        }
        for col,definition in migrations.items():
            if col not in cols: c.execute(f'ALTER TABLE transactions ADD COLUMN {col} {definition}')
        c.execute('CREATE INDEX IF NOT EXISTS idx_tx_source_file ON transactions(source_file)')
        if not c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            c.execute('INSERT INTO users(email,password_hash,role,created_at) VALUES(?,?,?,?)',('admin@muleguard.local',pw_hash(os.getenv('MULEGUARD_ADMIN_PASSWORD','ChangeMe123!')),'admin',now()))

def seed_bundled_dataset():
    """Load the supplied SOA workbooks once into an empty database."""
    data_path=ROOT/'data'/'soa_transactions.csv.gz'
    if os.getenv('MULEGUARD_SKIP_BUNDLED_DATA','').lower() in {'1','true','yes'} or not data_path.exists():
        return
    with connect() as c:
        if c.execute('SELECT COUNT(*) n FROM transactions').fetchone()['n'] > 0:
            return
        tx_sql='''INSERT OR IGNORE INTO transactions
        (transaction_id,source_account,destination_account,amount,currency,timestamp,transaction_type,status,channel,source_record_id,direction,statement_owner,balance,counterparty_ref,source_file,source_row,original_transaction_id,narration_ref,remarks_ref)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
        acct_sql='INSERT OR IGNORE INTO accounts(account_id,label,created_at) VALUES(?,?,?)'
        tx_batch=[]; acct_batch=[]; inserted=0; seed_time=now()
        with gzip.open(data_path,'rt',encoding='utf-8',newline='') as f:
            reader=csv.DictReader(f)
            for r in reader:
                owner=(r.get('__statement_owner') or '').strip()
                counter=(r.get('Ac_No') or '').strip()
                src=(r.get('__source_account') or '').strip()
                dst=(r.get('__destination_account') or '').strip()
                if not owner or not counter or not src or not dst: continue
                ts=(r.get('__timestamp_iso') or '').strip() or '1970-01-01'
                amount=float(r.get('__amount') or 0)
                txid=(r.get('__transaction_id') or '').strip()
                if not txid: continue
                holder='HOLDER:'+owner; counter_id='ACCT:'+counter
                acct_batch.extend([(holder,'Statement holder: '+owner,seed_time),(counter_id,'Counterparty account; name not supplied in this row',seed_time)])
                balance=float(r.get('Balance') or 0)
                tx_batch.append((txid,src,dst,amount,'INR',ts,'statement_'+(r.get('__direction') or 'unknown'),'recorded','SOA',r.get('Tran_ID') or '',r.get('__direction') or 'unknown',owner,balance,counter,r.get('__source_file') or '',int(r.get('__source_row') or 0),r.get('Tran_ID') or '',r.get('Narration') or '',r.get('Rmks') or ''))
                if len(tx_batch)>=2000:
                    c.executemany(acct_sql,acct_batch); c.executemany(tx_sql,tx_batch)
                    inserted+=len(tx_batch); tx_batch.clear(); acct_batch.clear()
            if tx_batch:
                c.executemany(acct_sql,acct_batch); c.executemany(tx_sql,tx_batch); inserted+=len(tx_batch)
        c.execute('INSERT INTO audit_logs(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)',
                  ('system','bundled_soa_dataset_loaded','dataset','uploaded-soa-workbooks',json.dumps({'transactions':inserted,'interpretation':'inferred direction from Dr_Amt/Cr_Amt and statement owner'}),now()))

init_db()
seed_bundled_dataset()

def current_user(authorization: Optional[str]=Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Sign in required. Demo account: admin@muleguard.local / ChangeMe123!')
    token=authorization[7:]; user=SESSIONS.get(token)
    if not user: raise HTTPException(401,'Session is invalid or expired. Sign in again.')
    return user
def require_roles(*roles):
    def dep(user=Depends(current_user)):
        if roles and user['role'] not in roles: raise HTTPException(403,'Your role cannot perform this action')
        return user
    return dep
class Login(BaseModel): email:str; password:str
class CaseIn(BaseModel): title:str=Field(min_length=2,max_length=200); summary:str=''; priority:str='Medium'; assigned_to:str=''
class CasePatch(BaseModel): status:Optional[str]=None; priority:Optional[str]=None; assigned_to:Optional[str]=None; closure_reason:Optional[str]=None
class LinkIn(BaseModel): entity_type:str; entity_id:str
class NoteIn(BaseModel): note:str=Field(min_length=1,max_length=5000)
class DemoSettings(BaseModel): force:bool=False
class ReportIn(BaseModel): title:str='MULEGUARD Investigation Summary'; case_id:Optional[str]=None

@app.get('/api/health')
def health(): return {'status':'ok','database':str(DB_PATH.name),'time':now()}

@app.get('/api/dataset/info')
def dataset_info():
    manifest_path=ROOT/'data'/'manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    with connect() as c:
        bundled=c.execute("SELECT COUNT(*) n FROM transactions WHERE source_file!=''").fetchone()['n']
        accounts=c.execute('SELECT COUNT(*) n FROM accounts').fetchone()['n']
    return {'name':manifest.get('dataset_name','Uploaded SOA workbook bundle'),'bundled_transactions':bundled,'accounts':accounts,'source_files':manifest.get('source_files',[]),'interpretation':manifest.get('interpretation',{}),'loaded':bundled>0}
@app.post('/api/auth/login')
def login(payload:Login):
    with connect() as c: u=c.execute('SELECT * FROM users WHERE lower(email)=lower(?)',(payload.email.strip(),)).fetchone()
    if not u or not pw_ok(payload.password,u['password_hash']): raise HTTPException(401,'Invalid email or password')
    token=secrets.token_urlsafe(32); user={'id':u['id'],'email':u['email'],'role':u['role']}; SESSIONS[token]=user; audit(u['email'],'login','user',str(u['id'])); return {'access_token':token,'token_type':'bearer','user':user}
@app.get('/api/auth/me')
def me(user=Depends(current_user)): return user
@app.post('/api/auth/logout')
def logout(authorization:Optional[str]=Header(default=None), user=Depends(current_user)):
    if authorization: SESSIONS.pop(authorization[7:],None)
    audit(user['email'],'logout','user',str(user['id'])); return {'ok':True}

@app.get('/api/dashboard/summary')
def summary():
    with connect() as c:
        accounts=c.execute('SELECT COUNT(*) n FROM accounts').fetchone()['n']; tx=c.execute('SELECT COUNT(*) n FROM transactions').fetchone()['n']; complaints=c.execute('SELECT COUNT(*) n FROM complaints').fetchone()['n']; cases=c.execute("SELECT COUNT(*) n FROM cases WHERE status!='Closed'").fetchone()['n']; alerts=c.execute("SELECT COUNT(*) n FROM alerts WHERE status='New'").fetchone()['n']; flagged=c.execute('SELECT COUNT(*) n FROM assessments WHERE score>=30').fetchone()['n']; high_tx=c.execute('SELECT COUNT(*) n FROM transactions t JOIN assessments a ON (t.source_account=a.account_id OR t.destination_account=a.account_id) WHERE a.score>=60').fetchone()['n'];
        dist={k:c.execute('SELECT COUNT(*) n FROM assessments WHERE band=?',(k,)).fetchone()['n'] for k in ['low','medium','high','critical']}
        value=c.execute('SELECT COALESCE(SUM(t.amount),0) n FROM transactions t JOIN assessments a ON (t.source_account=a.account_id OR t.destination_account=a.account_id) WHERE a.score>=60').fetchone()['n']
        bundled=c.execute("SELECT COUNT(*) n FROM transactions WHERE source_file!=''").fetchone()['n']
        synthetic=c.execute("SELECT COUNT(*) n FROM transactions WHERE transaction_id LIKE 'SYN-TX-%'").fetchone()['n']
    mode='bundled_soa_dataset' if bundled else 'synthetic' if synthetic else 'imported_or_empty'
    return {'accounts':accounts,'transactions':tx,'complaints':complaints,'correlated_complaints':complaints,'open_cases':cases,'new_alerts':alerts,'flagged_accounts':flagged,'high_risk_transactions':high_tx,'total_flagged_value':round(value,2),'risk_distribution':dist,'data_mode':mode}
def list_accounts(limit=3):
    with connect() as c: return [dict(x) for x in c.execute('SELECT account_id FROM accounts LIMIT ?', (limit,)).fetchall()]
@app.get('/api/dashboard/trends')
def trends():
    with connect() as c:
        rows=c.execute("SELECT substr(timestamp,1,10) day, COUNT(*) count, SUM(amount) amount FROM transactions GROUP BY day ORDER BY day DESC LIMIT 30").fetchall()
    return {'items':[dict(x) for x in reversed(rows)]}
@app.get('/api/accounts')
def accounts(q:str='',limit:int=100,offset:int=0):
    limit=max(1,min(limit,500));
    with connect() as c:
        rows=c.execute('SELECT a.account_id,a.label,COALESCE(s.score,0) score,COALESCE(s.band,\'low\') band,COALESCE(s.assessed_at,\'\') assessed_at FROM accounts a LEFT JOIN assessments s ON s.account_id=a.account_id WHERE a.account_id LIKE ? OR a.label LIKE ? ORDER BY score DESC LIMIT ? OFFSET ?',('%'+q+'%','%'+q+'%',limit,offset)).fetchall()
    return {'items':[dict(x) for x in rows],'limit':limit,'offset':offset}
@app.get('/api/accounts/{account_id}')
def account_detail(account_id:str):
    with connect() as c:
        a=c.execute('SELECT * FROM accounts WHERE account_id=?',(account_id,)).fetchone()
        if not a: raise HTTPException(404,'Account not found')
        risk=c.execute('SELECT * FROM assessments WHERE account_id=?',(account_id,)).fetchone()
        tx=c.execute('SELECT * FROM transactions WHERE source_account=? OR destination_account=? ORDER BY timestamp DESC LIMIT 100',(account_id,account_id)).fetchall()
        comps=c.execute('SELECT * FROM complaints WHERE account_id=? OR transaction_id IN (SELECT transaction_id FROM transactions WHERE source_account=? OR destination_account=?)',(account_id,account_id,account_id)).fetchall()
    return {'account':dict(a),'risk':dict(risk) if risk else None,'transactions':[dict(x) for x in tx],'complaints':[dict(x) for x in comps]}
@app.get('/api/transactions')
def transactions(q:str='',limit:int=100,offset:int=0,account_id:str='',min_amount:float=0):
    limit=max(1,min(limit,500)); where='WHERE (transaction_id LIKE ? OR source_account LIKE ? OR destination_account LIKE ?) AND amount>=?'; params=['%'+q+'%','%'+q+'%','%'+q+'%',min_amount]
    if account_id: where+=' AND (source_account=? OR destination_account=?)'; params += [account_id,account_id]
    with connect() as c:
        total=c.execute('SELECT COUNT(*) n FROM transactions '+where,params).fetchone()['n']; rows=c.execute('SELECT * FROM transactions '+where+' ORDER BY timestamp DESC LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    return {'items':[dict(x) for x in rows],'total':total,'limit':limit,'offset':offset}
@app.get('/api/transactions/{transaction_id}')
def transaction_detail(transaction_id:str):
    with connect() as c: r=c.execute('SELECT * FROM transactions WHERE transaction_id=?',(transaction_id,)).fetchone()
    if not r: raise HTTPException(404,'Transaction not found')
    return dict(r)
@app.post('/api/transactions/import')
async def import_transactions(file:UploadFile=File(...), user=Depends(require_roles('admin','analyst'))):
    if not file.filename.lower().endswith('.csv'): raise HTTPException(400,'Upload a CSV file')
    raw=await file.read()
    if len(raw)>25*1024*1024: raise HTTPException(413,'CSV exceeds 25 MB limit')
    try: reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))); fields={x.lower().strip() for x in (reader.fieldnames or [])}
    except Exception: raise HTTPException(400,'Could not read CSV header')
    aliases={'transaction_id':['transaction_id','txn_id','transactionid','id'],'source_account':['source_account','source_account_id','from_account','sender_account','src_account'],'destination_account':['destination_account','destination_account_id','to_account','receiver_account','dest_account'],'amount':['amount','transaction_amount','txn_amount'],'timestamp':['timestamp','transaction_date','date','datetime','transaction_timestamp']}
    mapping={}
    for key,names in aliases.items():
        found=next((n for n in names if n in fields),None)
        if not found: raise HTTPException(400,'Missing required column for '+key+'. Supported header aliases: '+', '.join(names))
        mapping[key]=found
    # map original header spelling
    orig={h.lower().strip():h for h in reader.fieldnames}
    imported=duplicates=rejected=0; errors=[]
    with connect() as c:
        for i,row in enumerate(reader,start=2):
            try:
                get=lambda k: (row.get(orig[mapping[k]]) or '').strip()
                tid,src,dst=get('transaction_id'),get('source_account'),get('destination_account'); amount=float(get('amount')); ts=get('timestamp')
                if not tid or not src or not dst or not ts or not math.isfinite(amount) or amount<0: raise ValueError('required field missing or invalid amount')
                try: ts=datetime.fromisoformat(ts.replace('Z','+00:00')).isoformat()
                except ValueError: ts=datetime.strptime(ts,'%d-%m-%Y %H:%M:%S').isoformat()
                currency=(row.get(orig.get('currency',''), '') or 'INR').strip() if orig.get('currency') else 'INR'
                status=(row.get(orig.get('status',''), '') or 'recorded').strip() if orig.get('status') else 'recorded'
                c.execute('INSERT OR IGNORE INTO transactions(transaction_id,source_account,destination_account,amount,currency,timestamp,status,source_record_id) VALUES(?,?,?,?,?,?,?,?)',(tid,src,dst,amount,currency,ts,status,tid))
                if c.execute('SELECT changes()').fetchone()[0]==0: duplicates+=1; continue
                imported+=1
                for aid in [src,dst]: c.execute('INSERT OR IGNORE INTO accounts(account_id,label,created_at) VALUES(?,?,?)',(aid,'Imported account',now()))
            except Exception as ex:
                rejected+=1
                if len(errors)<20: errors.append({'row':i,'error':str(ex)[:180]})
    audit(user['email'],'transaction_csv_import','dataset',file.filename,json.dumps({'imported':imported,'rejected':rejected,'duplicates':duplicates}))
    return {'filename':file.filename,'imported':imported,'rejected':rejected,'duplicates':duplicates,'errors':errors}
@app.post('/api/complaints/import')
async def import_complaints(file:UploadFile=File(...),user=Depends(require_roles('admin','analyst'))):
    if not file.filename.lower().endswith('.csv'): raise HTTPException(400,'Upload a CSV file')
    raw=await file.read()
    try: reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))); headers={h.lower().strip():h for h in (reader.fieldnames or [])}
    except Exception: raise HTTPException(400,'Could not read CSV')
    def col(*names): return next((headers[n] for n in names if n in headers),None)
    cid=col('complaint_id','case_id','id'); date=col('complaint_date','date','timestamp'); acct=col('account_id','source_account','reported_account'); txid=col('transaction_id','txn_id');
    if not cid or not date: raise HTTPException(400,'Complaint CSV needs complaint_id and complaint_date/date columns')
    imported=duplicates=0
    with connect() as c:
        for row in reader:
            comp=(row.get(cid) or '').strip(); dt=(row.get(date) or '').strip()
            if not comp or not dt: continue
            try: c.execute('INSERT OR IGNORE INTO complaints(complaint_id,complaint_date,reported_amount,transaction_id,account_id,recipient_id,status,description) VALUES(?,?,?,?,?,?,?,?)',(comp,dt,float((row.get(col('reported_amount','amount')) or '0').replace(',','') or 0), (row.get(txid) or '').strip() if txid else '', (row.get(acct) or '').strip() if acct else '', (row.get(col('recipient_id','recipient_account')) or '').strip() if col('recipient_id','recipient_account') else '', (row.get(col('status')) or 'open') if col('status') else 'open', (row.get(col('description','details')) or '') if col('description','details') else ''))
            except Exception: continue
            if c.execute('SELECT changes()').fetchone()[0]: imported+=1
            else: duplicates+=1
    audit(user['email'],'complaint_csv_import','dataset',file.filename,json.dumps({'imported':imported,'duplicates':duplicates})); return {'imported':imported,'duplicates':duplicates}
@app.get('/api/complaints')
def complaints(limit:int=100,offset:int=0):
    with connect() as c: rows=c.execute('SELECT * FROM complaints ORDER BY complaint_date DESC LIMIT ? OFFSET ?',(max(1,min(limit,500)),offset)).fetchall()
    return {'items':[dict(x) for x in rows]}
@app.get('/api/complaints/{complaint_id}/correlations')
def complaint_correlations(complaint_id:str):
    with connect() as c:
        cp=c.execute('SELECT * FROM complaints WHERE complaint_id=?',(complaint_id,)).fetchone()
        if not cp: raise HTTPException(404,'Complaint not found')
        matches=[]
        if cp['account_id']: matches += [dict(x) for x in c.execute('SELECT * FROM complaints WHERE complaint_id!=? AND account_id=?',(complaint_id,cp['account_id'])).fetchall()]
        if cp['transaction_id']: matches += [dict(x) for x in c.execute('SELECT * FROM complaints WHERE complaint_id!=? AND transaction_id=?',(complaint_id,cp['transaction_id'])).fetchall()]
    return {'complaint':dict(cp),'matches':matches,'method':'exact account_id and transaction_id equality; shared identifiers are leads, not proof'}
@app.post('/api/detection/run')
def run_detection(user=Depends(require_roles('admin','analyst','investigator'))):
    weights={'rapid_transfers':35,'behavioral_deviation':25,'complaint_correlation':30}; results=[]; t=now()
    with connect() as c:
        ids=[r['account_id'] for r in c.execute("""
            SELECT account_id FROM (
              SELECT source_account AS account_id FROM transactions
              UNION ALL SELECT destination_account AS account_id FROM transactions
            ) GROUP BY account_id HAVING COUNT(*)>=3
            UNION SELECT account_id FROM complaints WHERE account_id!=''
        """).fetchall()]
        for aid in ids:
            tx=c.execute('SELECT * FROM transactions WHERE source_account=? OR destination_account=? ORDER BY timestamp',(aid,aid)).fetchall()
            outgoing=[x for x in tx if x['source_account']==aid]; incoming=[x for x in tx if x['destination_account']==aid]
            # Efficiently count outgoing movements occurring within one hour after an incoming movement.
            from bisect import bisect_left
            def parse_time(value):
                try:
                    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
                    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                except Exception: return None
            parsed_tx_times=[parse_time(r['timestamp']) for r in tx]
            # Date-only rows cannot support a 60-minute rule. The supplied SOA Tran_Date
            # values have day precision only; do not treat midnight defaults as real times.
            temporal_precision=any(dt is not None and (dt.hour or dt.minute or dt.second) for dt in parsed_tx_times)
            incoming_times=sorted(x for x in (parse_time(r['timestamp']) for r in incoming) if x is not None) if temporal_precision else []
            outgoing_times=sorted(x for x in (parse_time(r['timestamp']) for r in outgoing) if x is not None) if temporal_precision else []
            rapid_out_count=0
            for ot in outgoing_times:
                left=bisect_left(incoming_times,ot-timedelta(hours=1))
                if left<len(incoming_times) and incoming_times[left]<=ot: rapid_out_count+=1
            rapid=temporal_precision and rapid_out_count>=2 and len(outgoing)>=3
            # Behavioral deviation only when sufficient baseline: at least 10 transactions; use robust IQR on amounts.
            deviation=False
            amounts=[float(x['amount']) for x in tx]
            if len(amounts)>=10:
                import statistics
                med=statistics.median(amounts); deviation=med>0 and any(x>med*5 for x in amounts)
            comp=c.execute('SELECT COUNT(*) n FROM complaints WHERE account_id=? OR transaction_id IN (SELECT transaction_id FROM transactions WHERE source_account=? OR destination_account=?)',(aid,aid,aid)).fetchone()['n']
            factors={'rapid_transfers':weights['rapid_transfers'] if rapid else 0,'behavioral_deviation':weights['behavioral_deviation'] if deviation else 0,'complaint_correlation':weights['complaint_correlation'] if comp>=2 else 0}
            score=min(100,sum(factors.values())); band='low' if score<30 else 'medium' if score<60 else 'high' if score<80 else 'critical'
            evidence={'transaction_ids':[x['transaction_id'] for x in tx[:100]],'outgoing_count':len(outgoing),'incoming_count':len(incoming),'rapid_outgoing_within_60m':rapid_out_count,'temporal_precision_available':temporal_precision,'complaint_count':comp,'rules_version':'transparent-rules-v2','limitations':'Illustrative rules; not validated as a real-world fraud classifier.'}
            c.execute('INSERT INTO assessments(account_id,score,band,factors,evidence,assessed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET score=excluded.score,band=excluded.band,factors=excluded.factors,evidence=excluded.evidence,assessed_at=excluded.assessed_at',(aid,score,band,json.dumps(factors),json.dumps(evidence),t))
            if score>=60:
                fp=hashlib.sha256(f'{aid}|{score}|{json.dumps(factors,sort_keys=True)}'.encode()).hexdigest()
                c.execute('INSERT OR IGNORE INTO alerts(fingerprint,account_id,score,severity,reason,status,created_at) VALUES(?,?,?,?,?,?,?)',(fp,aid,score,band,'; '.join(k.replace('_',' ') for k,v in factors.items() if v), 'New',t))
            results.append({'account_id':aid,'score':score,'band':band,'factors':factors,'evidence':evidence})
    audit(user['email'],'detection_run','assessment','all',json.dumps({'accounts_assessed':len(results)})); return {'assessed':len(results),'items':sorted(results,key=lambda x:x['score'],reverse=True),'weights':weights,'note':'Illustrative transparent rules; accounts with fewer than three recorded movements are not scored unless complaint-linked. Rapid-transfer scoring is skipped for date-only records because time-of-day is unavailable. Scores are investigative leads, not proof. SOA network directions are inferred from debit/credit fields.'}
@app.get('/api/detection/results')
def detection_results(limit:int=100):
    with connect() as c: rows=c.execute('SELECT * FROM assessments ORDER BY score DESC LIMIT ?',(max(1,min(limit,500)),)).fetchall()
    return {'items':[dict(x)|{'factors':json.loads(x['factors']),'evidence':json.loads(x['evidence'])} for x in rows]}
@app.get('/api/detection/results/{account_id}')
def detection_detail(account_id:str):
    with connect() as c:r=c.execute('SELECT * FROM assessments WHERE account_id=?',(account_id,)).fetchone()
    if not r: raise HTTPException(404,'No risk assessment found; run detection first')
    d=dict(r);d['factors']=json.loads(d['factors']);d['evidence']=json.loads(d['evidence']);return d
@app.get('/api/graph/network')
def graph_network(limit:int=200):
    with connect() as c:
        rows=c.execute('SELECT source_account,destination_account,COUNT(*) count,SUM(amount) amount FROM transactions GROUP BY source_account,destination_account LIMIT ?',(max(1,min(limit,1000)),)).fetchall()
        risks={x['account_id']:dict(x) for x in c.execute('SELECT account_id,score,band FROM assessments').fetchall()}
    nodes={}
    edges=[]
    for r in rows:
        for aid in [r['source_account'],r['destination_account']]:
            if aid not in nodes: nodes[aid]={'id':aid,'risk':risks.get(aid,{}).get('score',0),'band':risks.get(aid,{}).get('band','unassessed')}
        edges.append({'source':r['source_account'],'target':r['destination_account'],'transactions':r['count'],'amount':r['amount']})
    return {'nodes':list(nodes.values()),'edges':edges,'truncated':len(rows)>=limit,'note':'SOA edges are inferred from Dr_Amt/Cr_Amt and the statement-holder label; validate before operational use.'}
@app.get('/api/graph/money-trail')
def money_trail(source:str,destination:Optional[str]=None,max_depth:int=5):
    max_depth=max(1,min(max_depth,8))
    with connect() as c: rows=c.execute('SELECT transaction_id,source_account,destination_account,amount,currency,timestamp FROM transactions ORDER BY timestamp LIMIT 20000').fetchall()
    G=nx.DiGraph()
    for r in rows: G.add_edge(r['source_account'],r['destination_account'],transaction=dict(r))
    if source not in G: return {'source':source,'destination':destination,'paths':[],'message':'Source account has no recorded transactions'}
    paths=[]
    targets=[destination] if destination else [n for n in G.successors(source)]
    for dest in targets:
        if dest not in G: continue
        try:
            for p in nx.all_simple_paths(G,source,dest,cutoff=max_depth):
                if len(paths)>=100: break
                edges=[]
                for a,b in zip(p,p[1:]): edges.append(G[a][b]['transaction'])
                paths.append({'accounts':p,'transactions':edges})
        except (nx.NetworkXNoPath,nx.NodeNotFound): pass
    return {'source':source,'destination':destination,'max_depth':max_depth,'paths':paths,'note':'Paths include inferred SOA directions from debit/credit fields; they do not prove continued identity of funds.'}
@app.get('/api/cases')
def cases(status:Optional[str]=None,limit:int=100):
    with connect() as c:
        sql='SELECT * FROM cases'; params=[]
        if status: sql+=' WHERE status=?';params.append(status)
        sql+=' ORDER BY updated_at DESC LIMIT ?';params.append(max(1,min(limit,500)))
        rows=c.execute(sql,params).fetchall()
    return {'items':[dict(x) for x in rows]}
@app.post('/api/cases')
def create_case(payload:CaseIn,user=Depends(require_roles('admin','investigator','analyst'))):
    cid='CASE-'+datetime.now().strftime('%Y%m%d')+'-'+secrets.token_hex(3).upper();t=now()
    with connect() as c:c.execute('INSERT INTO cases(case_id,title,summary,status,priority,assigned_to,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(cid,payload.title,payload.summary,'New',payload.priority,payload.assigned_to,t,t))
    audit(user['email'],'case_created','case',cid);return {'case_id':cid,'title':payload.title,'status':'New','priority':payload.priority,'created_at':t}
@app.get('/api/cases/{case_id}')
def case_detail(case_id:str):
    with connect() as c:
        r=c.execute('SELECT * FROM cases WHERE case_id=?',(case_id,)).fetchone()
        if not r: raise HTTPException(404,'Case not found')
        links=c.execute('SELECT * FROM case_links WHERE case_id=?',(case_id,)).fetchall();notes=c.execute('SELECT * FROM notes WHERE case_id=? ORDER BY created_at',(case_id,)).fetchall()
    return {'case':dict(r),'links':[dict(x) for x in links],'notes':[dict(x) for x in notes]}
@app.patch('/api/cases/{case_id}')
def patch_case(case_id:str,payload:CasePatch,user=Depends(require_roles('admin','investigator'))):
    allowed={'New','Under Review','Investigation in Progress','Escalated','Closed'}
    data=payload.model_dump(exclude_none=True)
    if 'status' in data and data['status'] not in allowed: raise HTTPException(400,'Invalid case status')
    if not data: raise HTTPException(400,'No changes supplied')
    data['updated_at']=now()
    with connect() as c:
        if not c.execute('SELECT 1 FROM cases WHERE case_id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
        c.execute('UPDATE cases SET '+','.join(k+'=?' for k in data)+' WHERE case_id=?',list(data.values())+[case_id])
    audit(user['email'],'case_updated','case',case_id,json.dumps(data));return {'ok':True,'case_id':case_id,'changes':data}
@app.post('/api/cases/{case_id}/links')
def link_case(case_id:str,payload:LinkIn,user=Depends(require_roles('admin','investigator'))):
    if payload.entity_type not in {'account','transaction','complaint','alert'}: raise HTTPException(400,'Unsupported entity type')
    with connect() as c:
        if not c.execute('SELECT 1 FROM cases WHERE case_id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
        c.execute('INSERT OR IGNORE INTO case_links(case_id,entity_type,entity_id) VALUES(?,?,?)',(case_id,payload.entity_type,payload.entity_id))
    audit(user['email'],'case_evidence_linked','case',case_id,f'{payload.entity_type}:{payload.entity_id}');return {'ok':True}
@app.post('/api/cases/{case_id}/notes')
def add_note(case_id:str,payload:NoteIn,user=Depends(require_roles('admin','investigator'))):
    with connect() as c:
        if not c.execute('SELECT 1 FROM cases WHERE case_id=?',(case_id,)).fetchone(): raise HTTPException(404,'Case not found')
        c.execute('INSERT INTO notes(case_id,author,note,created_at) VALUES(?,?,?,?)',(case_id,user['email'],payload.note,now()))
    audit(user['email'],'case_note_added','case',case_id);return {'ok':True}
@app.get('/api/alerts')
def alerts(status:Optional[str]=None,limit:int=100):
    with connect() as c:
        sql='SELECT * FROM alerts';params=[]
        if status:sql+=' WHERE status=?';params.append(status)
        sql+=' ORDER BY score DESC,created_at DESC LIMIT ?';params.append(max(1,min(limit,500)));rows=c.execute(sql,params).fetchall()
    return {'items':[dict(x) for x in rows]}
@app.patch('/api/alerts/{alert_id}')
def patch_alert(alert_id:int,status:str,user=Depends(require_roles('admin','investigator'))):
    if status not in {'New','Reviewed','Dismissed','Escalated'}: raise HTTPException(400,'Invalid alert status')
    with connect() as c:
        cur=c.execute('UPDATE alerts SET status=? WHERE id=?',(status,alert_id))
        if not cur.rowcount: raise HTTPException(404,'Alert not found')
    audit(user['email'],'alert_status_changed','alert',str(alert_id),status);return {'ok':True}
@app.get('/api/audit-logs')
def audit_logs(limit:int=100,user=Depends(require_roles('admin'))):
    with connect() as c: rows=c.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?',(max(1,min(limit,500)),)).fetchall()
    return {'items':[dict(x) for x in rows]}
@app.post('/api/demo/load')
def load_demo(user=Depends(require_roles('admin','analyst'))):
    accounts=['SYN-1001','SYN-1002','SYN-1003','SYN-1004','SYN-1005','SYN-1006','SYN-1007','SYN-1008','SYN-1009','SYN-1010','SYN-1011','SYN-1012']
    with connect() as c:
        if c.execute("SELECT COUNT(*) n FROM transactions WHERE transaction_id LIKE 'SYN-TX-%'").fetchone()['n']>0: return {'message':'Synthetic demo data already exists','accounts':c.execute("SELECT COUNT(*) n FROM accounts WHERE account_id LIKE 'SYN-%'").fetchone()['n'],'transactions':c.execute("SELECT COUNT(*) n FROM transactions WHERE transaction_id LIKE 'SYN-TX-%'").fetchone()['n'],'complaints':c.execute("SELECT COUNT(*) n FROM complaints WHERE complaint_id LIKE 'SYN-C-%'").fetchone()['n']}
        for i,a in enumerate(accounts): c.execute('INSERT OR IGNORE INTO accounts(account_id,label,created_at) VALUES(?,?,?)',(a,'Synthetic demo account',now()))
        edges=[(0,1,95000,0),(1,2,90000,8),(2,3,85000,17),(3,4,82000,24),(0,5,12000,30),(5,6,11000,38),(6,7,10800,50),(8,9,450000,60),(9,10,5000,70),(10,11,4900,85),(1,8,15000,95),(4,7,7000,105),(2,5,9000,120),(3,6,8000,140),(8,11,3000,160),(0,2,5000,180),(5,7,2500,200),(9,11,1200,220),(0,3,6000,240),(1,4,5500,260),(6,8,4000,280),(7,10,3900,300),(2,9,2100,320),(10,4,800,340)]
        base=datetime.now(timezone.utc)-timedelta(days=12)
        for i,(a,b,amt,mins) in enumerate(edges):
            ts=(base+timedelta(minutes=mins)).isoformat(timespec='seconds')
            c.execute('INSERT OR IGNORE INTO transactions(transaction_id,source_account,destination_account,amount,currency,timestamp,transaction_type,status,channel,source_record_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(f'SYN-TX-{i+1:04}',accounts[a],accounts[b],amt,'INR',ts,'transfer','recorded','UPI' if i%2 else 'IMPS',f'SYN-ROW-{i+1}'))
        for i,(aid,tid,amt) in enumerate([(accounts[2],'SYN-TX-0003',90000),(accounts[3],'SYN-TX-0004',85000),(accounts[7],'SYN-TX-0012',10800),(accounts[4],'SYN-TX-0012',82000)]):
            c.execute('INSERT OR IGNORE INTO complaints(complaint_id,complaint_date,reported_amount,transaction_id,account_id,recipient_id,status,description) VALUES(?,?,?,?,?,?,?,?)',(f'SYN-C-{i+1:03}',(base+timedelta(days=i)).isoformat(),amt,tid,aid,'','open','Synthetic complaint record for demonstration only'))
        c.execute('INSERT OR IGNORE INTO cases(case_id,title,summary,status,priority,assigned_to,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',('SYN-CASE-001','Synthetic pass-through investigation','Demonstration case linking a synthetic multi-hop transaction chain.','Under Review','High','demo-investigator',now(),now()))
    audit(user['email'],'synthetic_demo_loaded','dataset','synthetic', 'Synthetic data only'); return {'accounts':len(accounts),'transactions':len(edges),'complaints':4,'cases':1,'label':'Synthetic demonstration data; not real customer records'}
@app.post('/api/demo/reset')
def reset_demo(user=Depends(require_roles('admin'))):
    with connect() as c:
        # Remove only records with synthetic identifiers; never touch imported rows.
        c.execute("DELETE FROM alerts WHERE account_id LIKE 'SYN-%'"); c.execute("DELETE FROM assessments WHERE account_id LIKE 'SYN-%'"); c.execute("DELETE FROM complaints WHERE complaint_id LIKE 'SYN-%'"); c.execute("DELETE FROM transactions WHERE transaction_id LIKE 'SYN-%'"); c.execute("DELETE FROM case_links WHERE case_id LIKE 'SYN-%'"); c.execute("DELETE FROM notes WHERE case_id LIKE 'SYN-%'"); c.execute("DELETE FROM cases WHERE case_id LIKE 'SYN-%'"); c.execute("DELETE FROM accounts WHERE account_id LIKE 'SYN-%'")
    audit(user['email'],'synthetic_demo_reset','dataset','synthetic');return {'ok':True,'message':'Synthetic records removed; imported records were preserved.'}
@app.post('/api/reports/generate')
def generate_report(payload:ReportIn,user=Depends(require_roles('admin','investigator','analyst'))):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
    except Exception: raise HTTPException(500,'Install reportlab to generate PDF reports')
    with connect() as c:
        summary=c.execute('SELECT COUNT(*) accounts FROM accounts').fetchone()['accounts']; tx=c.execute('SELECT COUNT(*) transactions FROM transactions').fetchone()['transactions']; comps=c.execute('SELECT COUNT(*) complaints FROM complaints').fetchone()['complaints']; risks=c.execute('SELECT account_id,score,band,factors FROM assessments ORDER BY score DESC LIMIT 20').fetchall(); recent=c.execute('SELECT transaction_id,source_account,destination_account,amount,currency,timestamp FROM transactions ORDER BY timestamp DESC LIMIT 15').fetchall()
        bundled=c.execute("SELECT COUNT(*) n FROM transactions WHERE source_file!=''").fetchone()['n']
        source_files=[x['source_file'] for x in c.execute("SELECT DISTINCT source_file FROM transactions WHERE source_file!='' ORDER BY source_file").fetchall()]
        case=None
        if payload.case_id: case=c.execute('SELECT * FROM cases WHERE case_id=?',(payload.case_id,)).fetchone()
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=letter,rightMargin=36,leftMargin=36,topMargin=36,bottomMargin=36); styles=getSampleStyleSheet(); scope_text='This report summarizes records currently stored in this prototype. Risk indicators are investigative leads, not proof of fraud. Synthetic demo records, if present, are illustrative only.'
    if bundled:
        safe_files=', '.join(source_files)
        scope_text+=f' Bundled SOA source: {bundled:,} rows across {len(source_files)} workbook(s): {safe_files}. Source/destination direction is inferred from AC_Name, Ac_No, Dr_Amt and Cr_Amt because explicit sender and receiver columns are absent. The supplied transaction dates have day-level precision, so rapid-transfer timing is not scored for these rows. Currency is provisionally displayed as INR; validate Crncy before operational use.'
    story=[Paragraph('MULEGUARD AI — Investigation Summary',styles['Title']),Paragraph('Generated '+now(),styles['Normal']),Spacer(1,12),Paragraph('Scope and limitations',styles['Heading2']),Paragraph(scope_text,styles['BodyText']),Spacer(1,10),Paragraph('Database summary',styles['Heading2']),Table([['Metric','Count'],['Accounts',str(summary)],['Transactions',str(tx)],['Complaints',str(comps)]],style=TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#16243B')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.4,colors.grey),('PADDING',(0,0),(-1,-1),7)])),Spacer(1,12),Paragraph('Highest risk assessments',styles['Heading2'])]
    rows=[['Account','Score','Band','Factors']]
    for r in risks: rows.append([r['account_id'],str(r['score']),r['band'],', '.join(k.replace('_',' ') for k,v in json.loads(r['factors']).items() if v)])
    story.append(Table(rows,repeatRows=1,style=TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#16243B')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.4,colors.grey),('FONTSIZE',(0,0),(-1,-1),8),('PADDING',(0,0),(-1,-1),5)])))
    story += [Spacer(1,12),Paragraph('Recent transactions',styles['Heading2'])]; tr=[['Transaction','Source','Destination','Amount','Timestamp']]
    for r in recent: tr.append([r['transaction_id'],r['source_account'],r['destination_account'],f"{r['amount']:,.2f} {r['currency']}",r['timestamp'][:19]])
    story.append(Table(tr,repeatRows=1,style=TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#16243B')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),.4,colors.grey),('FONTSIZE',(0,0),(-1,-1),7),('PADDING',(0,0),(-1,-1),4)])))
    if case: story += [Spacer(1,12),Paragraph('Selected case',styles['Heading2']),Paragraph(f"{case['case_id']} — {case['title']} ({case['status']})",styles['BodyText']),Paragraph(case['summary'],styles['BodyText'])]
    doc.build(story); buf.seek(0); rid='RPT-'+secrets.token_hex(4).upper()
    with connect() as c: c.execute('INSERT INTO reports(report_id,title,created_at,created_by) VALUES(?,?,?,?)',(rid,payload.title,now(),user['email']))
    audit(user['email'],'report_generated','report',rid,payload.title)
    return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="{rid}.pdf"'})
