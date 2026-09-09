const $ = id => document.getElementById(id);
const number = n => Number(n || 0).toLocaleString();
function cell(row, value) { const td = document.createElement('td'); td.textContent = value; row.append(td); }
function rows(id, data, values, columns) {
  $(id).replaceChildren();
  for (const item of data) { const tr = document.createElement('tr'); for (const value of values(item)) cell(tr, value); $(id).append(tr); }
  if (!data.length) { const tr=document.createElement('tr'); const td=document.createElement('td'); td.colSpan=columns; td.textContent='No records yet. Send a request through your proxy, then refresh.'; tr.append(td); $(id).append(tr); }
}
async function refresh() {
  $('refresh').disabled = true; $('state').textContent = 'Reading local records…';
  try {
    const response = await fetch('/data', {cache:'no-store'});
    if (!response.ok) throw new Error('Could not read data. Check that the proxy database exists, then reload this page.');
    const data = await response.json(), t=data.totals;
    $('metrics').replaceChildren();
    for (const [label,value] of [['Recorded requests',number(t.requests)],['Total tokens',number(t.tokens)],['Estimated cost','$'+Number(t.cost).toFixed(8)],['Cache hits',number(t.hits)],['Estimated savings','$'+Number(t.avoided).toFixed(8)],['Recorded errors',number(t.errors)]]) {
      const item=document.createElement('div'), name=document.createElement('p'), count=document.createElement('strong');
      name.textContent=label; count.textContent=value; item.append(name,count); $('metrics').append(item);
    }
    rows('keys',data.keys,k=>[k.app_name+' / '+k.id,k.is_active?'Active':'Revoked',k.providers.join(', ') || 'None',number(k.requests),number(k.tokens),'$'+Number(k.cost).toFixed(8)],6);
    rows('recent',data.recent,r=>[r.created_at,r.virtual_key_id,r.provider || 'Not routed',r.status_code,r.cache_status,number(r.total_tokens),Number(r.latency_ms).toFixed(1)+' ms'],7);
    $('state').textContent='Snapshot refreshed at '+new Date().toLocaleTimeString()+'. Manual refresh; not a live server-health check.';
  } catch(error) { $('state').textContent=error.message; }
  finally { $('refresh').disabled=false; }
}
$('refresh').addEventListener('click',refresh); refresh();
