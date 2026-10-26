/* MULEGUARD API bridge. Uses persisted backend data; the supplied design is preserved. */
(() => {
  const API = (window.MULEGUARD_CONFIG && window.MULEGUARD_CONFIG.API_BASE) || 'http://127.0.0.1:8000/api';
  const $ = (s, c=document) => c.querySelector(s);
  const $$ = (s, c=document) => [...c.querySelectorAll(s)];
  let token = localStorage.getItem('mg_token') || '';
  const headers = () => ({ ...(token ? {Authorization:`Bearer ${token}`} : {}) });
  async function req(path, opts={}) {
    const h = {...headers(), ...(opts.headers||{})};
    const r = await fetch(API+path, {...opts, headers:h});
    const data = await r.json().catch(()=>({detail:'Unexpected server response'}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }
  function toast(title, msg, type='info') {
    if (typeof window.showToast === 'function') { window.showToast(title,msg,type); return; }
    let el=$('#mg-toast'); if(!el){el=document.createElement('div');el.id='mg-toast';el.style.cssText='position:fixed;right:20px;bottom:20px;z-index:9999;background:#111a2b;border:1px solid #28d7f2;padding:14px 18px;border-radius:10px;color:#f1f5fc;max-width:360px;box-shadow:0 10px 40px #0008';document.body.append(el)}
    el.textContent=title+' — '+msg; setTimeout(()=>el.remove(),4500);
  }
  function addConnectionPill() {
    const top=$('#topbar'); if(!top || $('#mg-api-status')) return;
    const pill=document.createElement('div'); pill.id='mg-api-status'; pill.style.cssText='display:flex;align-items:center;gap:8px;margin-left:12px;font-size:11px;color:#96a5bb';
    pill.innerHTML='<span style="width:7px;height:7px;border-radius:50%;background:#ffb84d;display:inline-block"></span><span>API connecting…</span>';
    const inner=top.querySelector('.flex')||top; inner.append(pill);
    const actions=document.createElement('div'); actions.style.cssText='display:flex;gap:8px;align-items:center;margin-left:auto;padding:0 12px';
    actions.innerHTML='<button id="mg-load-demo" class="btn btn-primary" type="button" title="Adds synthetic records alongside the supplied SOA dataset">Load synthetic demo</button><label class="btn" for="mg-csv" style="cursor:pointer">Import CSV</label><input id="mg-csv" type="file" accept=".csv,text/csv" style="display:none">';
    inner.append(actions);
    $('#mg-load-demo').addEventListener('click',async()=>{try{const d=await req('/demo/load',{method:'POST'});toast('Demo dataset loaded',`${d.accounts} accounts, ${d.transactions} transactions, ${d.complaints} complaints stored.`, 'ok');await refreshAll();}catch(e){toast('Could not load demo data',e.message,'error')}});
    $('#mg-csv').addEventListener('change',async e=>{const f=e.target.files?.[0];if(!f)return;const fd=new FormData();fd.append('file',f);try{const d=await req('/transactions/import',{method:'POST',body:fd});toast('CSV import finished',`${d.imported} imported, ${d.rejected} rejected, ${d.duplicates} duplicates.`,d.imported?'ok':'info');await refreshAll()}catch(err){toast('CSV import failed',err.message,'error')}finally{e.target.value=''}});
  }
  function setStatus(ok, text) { const p=$('#mg-api-status');if(!p)return;p.innerHTML=`<span style="width:7px;height:7px;border-radius:50%;background:${ok?'#39d6a3':'#ff5268'};display:inline-block"></span><span>${text}</span>`; }
  function replaceCounter(label, value) {
    const cards=$$('.card-kpi');
    for(const card of cards){ if(card.innerText.toLowerCase().includes(label.toLowerCase())){ const n=card.querySelector('[data-count]');if(n){n.dataset.count=String(value);n.textContent=Number(value).toLocaleString('en-IN')} } }
  }
  async function refreshDashboard() {
    const d=await req('/dashboard/summary'); setStatus(true,d.data_mode==='bundled_soa_dataset'?`API connected · ${d.transactions.toLocaleString('en-IN')} SOA transactions`:`API connected · ${d.transactions.toLocaleString('en-IN')} transactions`);
    replaceCounter('Total Transactions',d.transactions);
    replaceCounter('Potential Mule',d.flagged_accounts);
    replaceCounter('High-Risk Transactions',d.high_risk_transactions);
    replaceCounter('Correlated Complaints',d.correlated_complaints);
    replaceCounter('Active Investigations',d.open_cases);
    replaceCounter('Total Accounts',d.accounts);
    // Avoid retaining the prototype's invented numbers in charts; label charts as backend-backed only when data exists.
    const donut=$('#donutLegend');
    if(d.risk_distribution && donut){const labels=[['Low risk (0–29)','low','#39D6A3'],['Medium risk (30–59)','medium','#FFB84D'],['High risk (60–79)','high','#FF8A55'],['Critical risk (80–100)','critical','#FF5268']];donut.innerHTML=labels.map(([name,key,color])=>`<div class="flex items-center gap-2.5 px-3 py-2 rounded-[8px]" style="background:#121D2F;border:1px solid var(--line)"><span style="width:10px;height:10px;border-radius:50%;background:${color}"></span><span class="text-[10.5px] text-t2 flex-1">${name}</span><span class="text-[10.5px] text-t1 num font-semibold">${(d.risk_distribution[key]||0).toLocaleString('en-IN')}</span></div>`).join('');}
    return d;
  }
  async function refreshTransactions(){
    const data=await req('/transactions?limit=50');const table=$('#txTable');if(!table)return;const body=table.querySelector('tbody');if(!body)return;
    body.innerHTML=data.items.map(t=>`<tr><td class="primary num">${esc(t.transaction_id)}</td><td class="num">${esc(t.source_account)}</td><td class="num">${esc(t.destination_account)}</td><td class="right num">${Number(t.amount).toLocaleString('en-IN',{maximumFractionDigits:2})} ${esc(t.currency||'')}</td><td class="num">${esc(t.timestamp||'')}</td><td><span class="badge b-neutral">${esc(t.status||'recorded')}</span></td></tr>`).join('')||'<tr><td colspan="6" style="padding:20px">No transactions yet. Load synthetic demo data or import a CSV.</td></tr>';
  }
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
  async function refreshAll(){try{await refreshDashboard();await refreshTransactions()}catch(e){setStatus(false,'API unavailable · start backend');}}
  // Replace demo login behavior with real API authentication.
  document.addEventListener('submit',async e=>{if(e.target?.id!=='authForm')return;e.preventDefault();e.stopImmediatePropagation();const email=$('#authEmail')?.value.trim(),password=$('#authPass')?.value;try{const d=await req('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});token=d.access_token;localStorage.setItem('mg_token',token);toast('Signed in',`Authenticated as ${d.user.email} (${d.user.role}).`,'ok');$('#authOverlay')?.classList.add('hidden');await refreshAll()}catch(err){const box=$('#authError');if(box){box.textContent=err.message;box.classList.remove('hidden')}else toast('Sign-in failed',err.message,'error')}} ,true);
  // Connect the existing report button to actual PDF generation and download.
  document.addEventListener('click',async e=>{const b=e.target.closest('#exportPdfBtn');if(!b)return;e.preventDefault();try{const r=await fetch(API+'/reports/generate',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({title:'MULEGUARD Investigation Summary'})});if(!r.ok){const x=await r.json().catch(()=>({}));throw new Error(x.detail||'Report generation failed')}const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='muleguard-investigation-report.pdf';a.click();URL.revokeObjectURL(a.href);toast('Report generated','Downloaded report built from current database records.','ok')}catch(err){toast('Report unavailable',err.message,'error')}},true);
  // Connect key investigation actions that were visual-only in the supplied prototype.
  document.addEventListener('click',async e=>{
    const b=e.target.closest('button'); if(!b)return;
    const label=(b.innerText||b.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
    if(label==='run detection batch'||label==='run detection'){
      e.preventDefault(); try{const d=await req('/detection/run',{method:'POST'});toast('Detection completed',`${d.assessed} accounts assessed. Open API docs for full explanations and evidence.`, 'ok');await refreshAll()}catch(err){toast('Detection failed',err.message,'error')}
    }
    if(label==='new case'||label==='create case from selection'||label==='create / link case'){
      e.preventDefault(); const title=prompt('Case title'); if(!title?.trim())return; const summary=prompt('Investigation summary (optional)')||'';
      try{const d=await req('/cases',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title.trim(),summary,priority:'Medium'})});toast('Case created',`${d.case_id} saved to the database.`,'ok');}catch(err){toast('Case creation failed',err.message,'error')}
    }
    if(label==='run correlation'){
      e.preventDefault();try{const d=await req('/complaints');toast('Complaint records loaded',`${d.items.length} complaint records available. Correlation is based on exact account/transaction identifiers.`, 'info')}catch(err){toast('Correlation unavailable',err.message,'error')}
    }
  },true);
  window.MuleGuardAPI={request:req,refresh:refreshAll};
  document.addEventListener('DOMContentLoaded',()=>{addConnectionPill();refreshAll();});
  if(document.readyState!=='loading'){addConnectionPill();refreshAll()}
})();
