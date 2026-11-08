'use strict';
const $=id=>document.getElementById(id);
const number=(n,d=2)=>Number(n).toLocaleString('zh-CN',{maximumFractionDigits:d});
let snapshot=null, first=true, notify=false, cursors={spot:0,futures:0}, saving=false;
const marketName=market=>market==='futures'?'USDT 永续合约':'现货';
function settingsMode(){const mode=$('mode').value;$('symbol').disabled=mode==='futures';$('symbol').required=mode!=='futures';$('futuresSymbol').disabled=mode==='spot';$('futuresSymbol').required=mode!=='spot';}
function error(message=''){$('error').textContent=message;$('error').hidden=!message;}
function renderRows(id,rows){

 const fragment=document.createDocumentFragment();
 for(const trade of rows.slice(0,100)){
  const tr=document.createElement('tr');tr.className=trade.side+(trade.large?' large':'');
  const time=new Date(trade.time).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})+'.'+String(trade.time%1000).padStart(3,'0');
  for(const value of [time,trade.side==='buy'?'主动买入':'主动卖出',number(trade.price,8),number(trade.quantity,8),number(trade.amount)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
  fragment.append(tr);
 }
 if(!fragment.childNodes.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=5;td.className='empty';td.textContent='暂无记录';tr.append(td);fragment.append(tr);}
 $(id).replaceChildren(fragment);
}
function render(s){
 const oldConfig=JSON.stringify(snapshot?.config);snapshot=s;
 const enabled=s.config.mode==='both'?['spot','futures']:[s.config.mode];
 const entries=enabled.flatMap(market=>s.markets[market].config.symbols.map(symbol=>({symbol,market,key:market+'-'+symbol})));
 if(first){$('mode').value=s.config.mode;$('symbol').value=s.config.symbols.join(', ');$('futuresSymbol').value=s.config.futures_symbols.join(', ');$('threshold').value=s.config.large_trade_usdt;$('futuresThreshold').value=s.config.futures_large_trade_usdt;settingsMode();}
 if(oldConfig!==JSON.stringify(s.config)){
  loadHistorySymbols();
  $('groups').replaceChildren();
  for(const {symbol,market,key} of entries){
   const section=document.createElement('section');section.className='coin-group';section.dataset.symbol=symbol;
   const head=document.createElement('div');head.className='summary';
   const title=document.createElement('h2');title.textContent=marketName(market)+' · '+symbol;head.append(title);
   const latest=document.createElement('span');latest.id='headline-'+key;head.append(latest);section.append(head);
   const columns=document.createElement('div');columns.className='columns';
   for(const [kind,titleText,route] of [['trades','最新成交','trades'],['large','大额成交','large']]){
    const panel=document.createElement('section');panel.className='panel';
    const panelHead=document.createElement('div');panelHead.className='panel-head';
    const title=document.createElement('h3');title.textContent=titleText+(market==='futures'?'（聚合）':'');
    const link=document.createElement('a');link.href='/api/export/'+market+'/'+symbol+'/'+route+'.csv';link.textContent='导出 '+symbol+' CSV';panelHead.append(title,link);panel.append(panelHead);
    const scroll=document.createElement('div');scroll.className='scroll';const table=document.createElement('table');const thead=document.createElement('thead');const tr=document.createElement('tr');
    for(const name of ['时间','方向','价格','数量','金额（USDT）']){const th=document.createElement('th');th.textContent=name;tr.append(th);}thead.append(tr);table.append(thead);
    const tbody=document.createElement('tbody');tbody.id=kind+'-'+key;table.append(tbody);scroll.append(table);panel.append(scroll);columns.append(panel);
   }
   section.append(columns);$('groups').append(section);
  }
 }
 $('status').textContent=({live:'● 后台监控中 · 日志已开启',partial:'部分市场连接异常 · 其他市场仍在采集',connecting:'正在连接币安…',stopping:'正在停止并保存日志…',stopped:'已停止监控 · 可查询历史'})[s.status]||'采集异常';
 $('status').className=s.status==='live'?'live':'';error([s.error,s.log_error].filter(Boolean).join('；'));
 $('thresholdLabel').textContent='现货门槛 ≥ '+number(s.config.large_trade_usdt)+' USDT ｜ 合约门槛 ≥ '+number(s.config.futures_large_trade_usdt)+' USDT';
 $('coins').replaceChildren();
 for(const {symbol,market,key} of entries){const tag=document.createElement('span');const child=s.markets[market],received=child.last_received[symbol];tag.textContent=marketName(market)+' '+symbol+' · '+child.status+' · '+(received?'最近收到 '+new Date(received).toLocaleTimeString():'等待成交（请确认交易对有效）');$('coins').append(tag);
  const rows=child.trades.filter(t=>t.symbol===symbol),large=child.large_trades.filter(t=>t.symbol===symbol);
  renderRows('trades-'+key,rows);renderRows('large-'+key,large);
  const last=large[0],headline=$('headline-'+key);headline.textContent=last?(last.side==='buy'?'大额主动买入 ':'大额主动卖出 ')+number(last.amount)+' USDT':'等待大额成交';headline.className=last?.side||'muted';
 }
 $('last').textContent=enabled.map(m=>marketName(m)+' 已保存序号：'+s.markets[m].cursor).join(' ｜ ');
 $('logPath').textContent='日志：'+s.log_path;
 for(const market of enabled){
  const large=s.markets[market].large_trades;
  if(!first&&notify&&Notification.permission==='granted'){
   const fresh=large.filter(t=>t.seq>cursors[market]);
   if(fresh.length)new Notification(marketName(market)+' '+fresh.length+' 笔大额成交',{body:fresh.slice(0,3).map(t=>t.symbol+' '+(t.side==='buy'?'买入':'卖出')+' '+number(t.amount)+' USDT').join('\n'),tag:market+'-large-trades'});
  }
  cursors[market]=Math.max(cursors[market],...large.map(t=>t.seq));
 }
 first=false;
}
$('settings').onsubmit=async event=>{
 event.preventDefault();saving=true;$('save').disabled=true;
 try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('mode').value,symbols:$('symbol').value,futures_symbols:$('futuresSymbol').value,large_trade_usdt:Number($('threshold').value),futures_large_trade_usdt:Number($('futuresThreshold').value)}),signal:AbortSignal.timeout(120000)});const body=await r.json();if(!r.ok)throw Error(body.error||'保存失败');error();}catch(e){error(e.message);}finally{saving=false;$('save').disabled=false;}
};
$('notify').onclick=async()=>{if(!('Notification'in window)){error('浏览器不支持桌面通知');return;}notify=(await Notification.requestPermission())==='granted';$('notify').textContent=notify?'通知已开启':'通知未授权';};
async function poll(){try{if(!saving){const response=await fetch('/api/state',{signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('服务返回 '+response.status);const s=await response.json();if(!saving)render(s);}}catch(e){$('status').textContent='本地服务连接中断';$('status').className='';error(e.message+'；请确认后台程序仍在运行。');}finally{setTimeout(poll,1000);}}
poll();

function renderPositions(data){
 $('positionsStatus').textContent=!data.rows.length?'仅合约支持持仓分布，请在上方启用合约监控。':data.active?'按当前合约币种显示 · 自动更新':'监控已暂停 · 以下为缓存数据';
 $('positionsRows').replaceChildren();
 for(const row of data.rows){
  const card=document.createElement('section');card.className='panel';card.style.padding='18px';
  const title=document.createElement('h3');title.textContent=row.symbol+(row.loading?' · 更新中…':'');card.append(title);
  for(const [key,label] of [['open_interest','总持仓量'],['top_positions','大户多空持仓占比'],['global_accounts','全市场多空账户占比']]){
   const m=row.metrics[key]||{};
   const p=document.createElement('p');p.textContent=label+'：';
   if(!m.time){p.append('暂无数据');}
   else if(key==='open_interest'){p.append(number(m.value,8)+' '+row.symbol.slice(0,-4)+'（合约标的单位）');}
   else{
    const long=document.createElement('span');long.className='buy';long.textContent='多 '+number(m.long*100)+'%';
    const short=document.createElement('span');short.className='sell';short.textContent='空 '+number(m.short*100)+'%';
    p.append(long,' / ',short,' ｜ 多空比 '+number(m.ratio,4));
   }
   card.append(p);
   if(m.time && key!=='open_interest'){
    const bar=document.createElement('div');bar.style.cssText='height:8px;display:flex;background:#263347;border-radius:4px;overflow:hidden';bar.setAttribute('aria-hidden','true');
    for(const [value,color] of [[m.long,'#4bd5ad'],[m.short,'#ff7485']]){const part=document.createElement('span');part.style.width=(value*100)+'%';part.style.background=color;bar.append(part);}card.append(bar);
   }
   const info=document.createElement('p');info.className='muted';info.style.fontSize='12px';
   const stale=m.time && Date.now()-m.time>(key==='open_interest'?180000:900000);
   info.textContent=(m.time?'数据时间 '+new Date(m.time).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'等待接口返回')+(stale?' · 数据已过期':'')+(m.error?' · '+m.error+(m.time?'（保留上次数据）':''):'');
   if(stale||m.error)info.style.color='#ffb170';card.append(info);
  }
  $('positionsRows').append(card);
 }
}
async function pollPositions(){
 try{const r=await fetch('/api/positions',{signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('服务返回 '+r.status);renderPositions(await r.json());}
 catch(e){$('positionsStatus').textContent='持仓更新失败，已有数据可能过期：'+e.message;}
 finally{setTimeout(pollPositions,5000);}
}
pollPositions();

let searchQuery=null, searchPage=1, searchPages=1, searching=false;
const chinaInput=stamp=>new Date(stamp+8*3600000).toISOString().slice(0,19);
$('searchEnd').value=chinaInput(Date.now());
$('searchStart').value=chinaInput(Date.now()-3600000);
async function loadHistorySymbols(){
 const market=$('searchMarket').value;
 try{const r=await fetch('/api/symbols?market='+market);if(!r.ok)throw Error('无法加载历史币种');const symbols=await r.json();if(market!==$('searchMarket').value)return;
 const selected=$('searchSymbol').value;$('searchSymbol').replaceChildren();
 for(const symbol of symbols){const option=document.createElement('option');option.value=symbol;option.textContent=symbol;$('searchSymbol').append(option);}
 if(symbols.includes(selected))$('searchSymbol').value=selected;
 else if(symbols.includes('XRPUSDT'))$('searchSymbol').value='XRPUSDT';
 }catch(e){$('searchMessage').textContent=e.message;}
}
async function searchHistory(page,query){
 if(searching)return;searching=true;
 $('searchButton').disabled=true;$('prevPage').disabled=true;$('nextPage').disabled=true;
 $('searchResults').hidden=true;$('searchMessage').textContent='正在查询…';
 try{const params=new URLSearchParams({...query,page});const response=await fetch('/api/search?'+params,{signal:AbortSignal.timeout(30000)});const result=await response.json();if(!response.ok)throw Error(result.error||'查询失败');
 searchQuery={...query,cutoff:result.cutoff};searchPage=result.page;searchPages=result.pages;
 renderRows('searchRows',result.rows);
 $('searchMessage').textContent=marketName(query.market)+' '+query.symbol+' ｜ '+query.start.replace('T',' ')+' 至 '+query.end.replace('T',' ')+'（北京时间，结束时间不含） ｜ '+(query.kind==='large'?'仅大额成交':'全部成交')+' ｜ 共 '+number(result.total,0)+' 条';
 $('searchPage').textContent='第 '+searchPage+' / '+searchPages+' 页，每页 100 条';
 $('searchExport').href='/api/search.csv?'+new URLSearchParams(searchQuery);
 $('searchResults').hidden=false;
 }catch(e){$('searchMessage').textContent='查询失败：'+e.message;}
 finally{searching=false;$('searchButton').disabled=false;$('prevPage').disabled=searchPage<=1;$('nextPage').disabled=searchPage>=searchPages;}
}
$('searchForm').onsubmit=event=>{event.preventDefault();searchHistory(1,{market:$('searchMarket').value,symbol:$('searchSymbol').value,start:$('searchStart').value,end:$('searchEnd').value,kind:$('searchKind').value});};
$('searchMarket').onchange=loadHistorySymbols;
$('mode').onchange=settingsMode;
$('prevPage').onclick=()=>searchHistory(searchPage-1,searchQuery);
$('nextPage').onclick=()=>searchHistory(searchPage+1,searchQuery);
loadHistorySymbols();

let controlling=false;
async function controlMonitor(action){
 if(controlling)return;controlling=true;$('startMonitor').disabled=$('stopMonitor').disabled=true;
 try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action}),signal:AbortSignal.timeout(120000)});const data=await r.json();if(!r.ok)throw Error(data.error||'操作失败');error();}catch(e){error('控制操作未确认完成：'+e.message+'。请查看服务状态。');}finally{controlling=false;$('startMonitor').disabled=$('stopMonitor').disabled=false;}
}
$('startMonitor').onclick=()=>controlMonitor('start');
$('stopMonitor').onclick=()=>controlMonitor('stop');

function bytes(value){const units=['B','KiB','MiB','GiB','TiB'];let i=0;while(value>=1024&&i<units.length-1){value/=1024;i++;}return number(value,2)+' '+units[i];}
let loadingStorage=false;
async function loadStorage(manual=false){
 if(loadingStorage)return;loadingStorage=true;$('refreshStorage').disabled=true;
 try{const r=await fetch('/api/storage'+(manual?'?refresh=1':''),{signal:AbortSignal.timeout(30000)});const s=await r.json();if(!r.ok)throw Error(s.error||'统计失败');
 $('storageTotal').textContent=bytes(s.total_bytes);
 $('storageFiles').textContent=number(s.file_count,0)+' 个文件';
 $('diskFree').textContent=bytes(s.disk_free_bytes);
 $('diskCapacity').textContent='磁盘总容量 '+bytes(s.disk_total_bytes)+'，剩余 '+number(s.disk_free_percent,1)+'%';
 $('storageGroups').replaceChildren();
 for(const [key,label] of [['spot','现货日志'],['futures','合约日志'],['legacy','历史备份'],['runtime','运行日志及旧数据库']]){const item=document.createElement('span');item.textContent=label+'：'+bytes(s.groups[key].bytes);$('storageGroups').append(item);}
 $('storageKinds').textContent='CSV '+bytes(s.kinds.csv)+' ｜ 数据库（含 WAL）'+bytes(s.kinds.database)+' ｜ 运行日志 '+bytes(s.kinds.service)+' ｜ 其他 '+bytes(s.kinds.other);
 $('storageMessage').textContent=(s.low_space?'⚠ 磁盘空间不足：剩余低于 5 GiB 或 10%，请及时备份和清理日志。 ':'')+(s.partial?'部分文件无法统计（'+s.error_count+' 项），当前数值可能偏低。 ':'')+'统计时间：'+new Date(s.updated_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});
 $('storageMessage').style.color=s.low_space||s.partial?'#ffb170':'#91a3bc';
 }catch(e){$('storageMessage').textContent='空间统计更新失败，已有数字可能过期：'+e.message;$('storageMessage').style.color='#ffb170';}
 finally{loadingStorage=false;$('refreshStorage').disabled=false;}
}
$('refreshStorage').onclick=()=>loadStorage(true);
loadStorage();setInterval(()=>loadStorage(),30000);
