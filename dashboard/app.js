const $ = id => document.getElementById(id);
const number = n => Number(n || 0).toLocaleString();
const money = n => '$' + Number(n || 0).toFixed(8);
let csrf = '', grantId = null, busy = false, confirmWork = null;
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function api(path, data) {
  const options = {cache: 'no-store'};
  if (data !== undefined) Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body:JSON.stringify(data)});
  const response = await fetch(path, options), result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.error || 'Operation failed. Refresh and try again.');
  return result;
}
function page() {
  const name = ['keys','maintenance'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  for (const section of document.querySelectorAll('.page')) section.hidden = section.id !== 'page-' + name;
  for (const link of document.querySelectorAll('[data-page]')) {
    if (link.dataset.page === name) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
  }
  document.title = 'ProxyLLM · ' + ({overview:'Overview',keys:'Virtual keys',maintenance:'Maintenance'})[name];
}
function rowValues(row, values) { for (const value of values) row.append(element('td', value)); }
function empty(target, columns, message) {
  const row = element('tr',''), cell = element('td', message); cell.colSpan = columns; row.append(cell); target.append(row);
}
function button(text, action) {
  const b = element('button', text, 'secondary'); b.addEventListener('click', action); return b;
}
async function usage() {
  const id = $('usage-key').value;
  const data = await api('/admin/usage' + (id ? '?key_id=' + encodeURIComponent(id) : ''));
  const total = field => data.reduce((sum, item) => sum + Number(item[field] || 0), 0);
  $('key-usage').replaceChildren();
  for (const [label,value] of [['Requests',number(total('requests'))],['Tokens',number(total('total_tokens'))],['Estimated cost',money(total('estimated_cost_usd'))],['Cache hits',number(total('cache_hits'))],['Estimated savings',money(total('cost_avoided_usd'))]]) {
    const div=element('div',''); div.append(element('span',label),element('strong',value)); $('key-usage').append(div);
  }
  return data;
}
async function backups() {
  const data = await api('/admin/backups'); $('snapshots').replaceChildren(new Option('Select a snapshot', ''));
  for (const name of data.snapshots) $('snapshots').append(new Option(name,name));
}
async function refresh() {
  $('refresh').disabled=true; $('state').textContent='Reading local records…';
  try {
    csrf = (await api('/session')).csrf;
    const data = await api('/data'), t = data.totals;
    $('metrics').replaceChildren();
    for (const [label,value] of [['Recorded requests',number(t.requests)],['Provider tokens',number(t.tokens)],['Estimated cost',money(t.cost)],['Cache hits',number(t.hits)],['Estimated savings',money(t.avoided)],['Recorded errors',number(t.errors)]]) {
      const div=element('div',''); div.append(element('p',label),element('strong',value)); $('metrics').append(div);
    }
    $('keys').replaceChildren(); const selected=$('usage-key').value;
    $('usage-key').replaceChildren(new Option('All keys',''));
    for (const key of data.keys) {
      const row=element('tr',''), name=element('td',key.app_name), status=element('td',''), actions=element('td','');
      name.append(element('span','#'+key.id,'subtle')); status.append(element('span',key.is_active?'Active':'Revoked','badge'));
      actions.append(button('View usage',()=>{ $('usage-key').value=String(key.id); report(usage); }));
      if (key.is_active) actions.append(button('Grant access',()=>openKey(key)),button('Revoke',()=>confirmAction('Revoke key',`Revoke access for ${key.app_name} (#${key.id})? Requests using this key will be rejected. Usage records remain.`,async()=>api(`/admin/keys/${key.id}/revoke`,{confirm:true}),true)));
      row.append(name,status,element('td',key.providers.join(', ') || 'None'),actions); $('keys').append(row);
      $('usage-key').append(new Option(key.app_name+' / #'+key.id,String(key.id)));
    }
    if ([...$('usage-key').options].some(o=>o.value===selected)) $('usage-key').value=selected;
    if (!data.keys.length) empty($('keys'),4,'No virtual keys yet. Create a key to get started.');
    $('recent').replaceChildren();
    for (const item of data.recent) { const row=element('tr',''); rowValues(row,[item.created_at,'#'+item.virtual_key_id,item.provider||'Not routed',item.status_code,item.cache_status,number(item.total_tokens),Number(item.latency_ms).toFixed(1)+' ms']); $('recent').append(row); }
    if (!data.recent.length) empty($('recent'),7,'No recorded requests yet.');
    await usage(); await backups();
    $('state').textContent='Updated '+new Date().toLocaleTimeString()+' · Manual refresh';
  } catch(error) { $('state').textContent=error.message; }
  finally { $('refresh').disabled=false; }
}
async function report(work) {
  try { await work(); } catch(error) { $('state').textContent=error.message; }
}
function openKey(key=null) {
  if (busy) return;
  grantId=key?.id ?? null; $('key-form').reset(); $('key-error').textContent='';
  $('app-field').hidden=grantId!==null; $('app-name').required=grantId===null;
  $('key-title').textContent=grantId===null?'Create virtual key':'Grant provider access';
  $('key-description').textContent=key?`Add access for ${key.app_name} (#${key.id}).`:'Choose the application’s initial provider access.';
  $('key-submit').textContent=grantId===null?'Create key':'Grant access'; $('key-dialog').showModal();
}
$('key-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(busy) return; busy=true; $('key-submit').disabled=true; $('key-cancel').disabled=true;
  try {
    const data={provider:$('provider').value,credential:$('credential').value};
    if(grantId===null) data.app_name=$('app-name').value.trim();
    const result=await api(grantId===null?'/admin/keys':`/admin/keys/${grantId}/grant`,data);
    $('key-dialog').close();
    if(result.secret) { $('new-secret').value=result.secret; $('copy-state').textContent=''; $('secret-dialog').showModal(); }
    await refresh(); $('state').textContent=result.message || 'Key created.';
  } catch(error) { $('key-error').textContent=error.message; }
  finally { busy=false; $('key-submit').disabled=false; $('key-cancel').disabled=false; }
});
function confirmAction(title,message,work,danger=false) {
  if(busy) return;
  $('confirm-title').textContent=title; $('confirm-message').textContent=message; $('confirm-error').textContent='';
  $('confirm-action').textContent=title; $('confirm-action').className=danger?'danger':'';
  confirmWork=work; $('confirm-dialog').showModal();
}
$('confirm-action').addEventListener('click',async()=>{
  if(busy || !confirmWork) return; busy=true; $('confirm-action').disabled=true; $('confirm-cancel').disabled=true;
  try { const result=await confirmWork(); $('confirm-dialog').close(); await refresh(); $('state').textContent=result.message+(result.snapshot?' '+result.snapshot:''); }
  catch(error) { $('confirm-error').textContent=error.message; }
  finally { busy=false; $('confirm-action').disabled=false; $('confirm-cancel').disabled=false; }
});
for(const id of ['key-dialog','confirm-dialog']) $(id).addEventListener('cancel',event=>{if(busy) event.preventDefault();});
$('key-cancel').onclick=()=>$('key-dialog').close(); $('confirm-cancel').onclick=()=>$('confirm-dialog').close();
$('secret-close').onclick=()=>$('secret-dialog').close();
$('secret-dialog').addEventListener('cancel',event=>event.preventDefault());
$('secret-dialog').addEventListener('close',()=>{$('new-secret').value='';$('state').textContent='Key created. Secret dismissed.';});
window.addEventListener('pagehide',()=>{$('new-secret').value='';});
$('copy-secret').onclick=async()=>{try{await navigator.clipboard.writeText($('new-secret').value);$('copy-state').textContent='Copied. Store it somewhere safe.';}catch{$('new-secret').select();$('copy-state').textContent='Select and copy the key manually.';}};
$('create-open').onclick=()=>openKey(); $('overview-create').onclick=()=>{location.hash='keys';openKey();};
$('refresh').onclick=refresh; $('usage-key').onchange=()=>report(usage);
$('export').onclick=()=>report(async()=>{const data=await usage(); const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})); const link=element('a','');link.href=url;link.download='proxyllm-usage.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('backup-create').onclick=()=>{const kind=$('backup-kind').value;confirmAction('Create backup',kind==='hourly'?'Create an hourly snapshot and apply retention? Older hourly snapshots may be deleted after retained copies pass verification.':'Create and verify a maintenance snapshot?',()=>api('/admin/backups',{kind,confirm:true}));};
$('backup-verify').onclick=()=>report(async()=>{if(!$('snapshots').value) throw new Error('Select a snapshot first.');$('backup-verify').disabled=true;try{const result=await api('/admin/backups/verify',{snapshot:$('snapshots').value});$('state').textContent=result.message;}finally{$('backup-verify').disabled=false;}});
$('cache-preview').onclick=()=>report(async()=>{const result=await api('/admin/cache');$('cache-count').textContent=number(result.expired)+' expired entries';$('cache-delete').disabled=!result.expired;});
$('cache-delete').onclick=()=>confirmAction('Delete expired entries','Create a verified backup, then delete entries that are expired at execution time? The count may have changed since preview.',async()=>{const result=await api('/admin/cache/cleanup',{confirm:true});$('cache-count').textContent='Cleanup complete. Preview again to check remaining entries.';$('cache-delete').disabled=true;return result;});
window.addEventListener('hashchange',page); page(); refresh();
