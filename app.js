'use strict';
const $=id=>document.getElementById(id);
const number=(n,d=2)=>Number(n).toLocaleString('zh-CN',{maximumFractionDigits:d});
let snapshot=null, first=true, notify=false, cursor=0, saving=false;
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
 const oldSymbols=snapshot?.config.symbols.join(',');snapshot=s;
 if(first){$('symbol').value=s.config.symbols.join(', ');$('threshold').value=s.config.large_trade_usdt;}
 if(oldSymbols!==s.config.symbols.join(',')){
  loadHistorySymbols();
  $('groups').replaceChildren();
  for(const symbol of s.config.symbols){
   const section=document.createElement('section');section.className='coin-group';section.dataset.symbol=symbol;
   const head=document.createElement('div');head.className='summary';
   const title=document.createElement('h2');title.textContent=symbol;head.append(title);
   const latest=document.createElement('span');latest.id='headline-'+symbol;head.append(latest);section.append(head);
   const columns=document.createElement('div');columns.className='columns';
   for(const [kind,titleText,route] of [['trades','最新成交','trades'],['large','大额成交','large']]){
    const panel=document.createElement('section');panel.className='panel';
    const panelHead=document.createElement('div');panelHead.className='panel-head';
    const title=document.createElement('h3');title.textContent=titleText;
    const link=document.createElement('a');link.href='/api/export/'+symbol+'/'+route+'.csv';link.textContent='导出 '+symbol+' CSV';panelHead.append(title,link);panel.append(panelHead);
    const scroll=document.createElement('div');scroll.className='scroll';const table=document.createElement('table');const thead=document.createElement('thead');const tr=document.createElement('tr');
    for(const name of ['时间','方向','价格','数量','金额（USDT）']){const th=document.createElement('th');th.textContent=name;tr.append(th);}thead.append(tr);table.append(thead);
    const tbody=document.createElement('tbody');tbody.id=kind+'-'+symbol;table.append(tbody);scroll.append(table);panel.append(scroll);columns.append(panel);
   }
   section.append(columns);$('groups').append(section);
  }
 }
 $('status').textContent=({live:'● 后台监控中 · 日志已开启',connecting:'正在连接币安…',stopping:'正在停止并保存日志…',stopped:'已停止监控 · 可查询历史'})[s.status]||'采集异常';
 $('status').className=s.status==='live'?'live':'';error([s.error,s.log_error].filter(Boolean).join('；'));
 $('thresholdLabel').textContent='当前单笔门槛 ≥ '+number(s.config.large_trade_usdt)+' USDT';
 $('coins').replaceChildren();
 for(const symbol of s.config.symbols){const tag=document.createElement('span');const received=s.last_received[symbol];tag.textContent=symbol+' · '+(received?'最近收到 '+new Date(received).toLocaleTimeString():'等待成交（请确认交易对有效）');$('coins').append(tag);}
 for(const symbol of s.config.symbols){
  const rows=s.trades.filter(t=>t.symbol===symbol),large=s.large_trades.filter(t=>t.symbol===symbol);
  renderRows('trades-'+symbol,rows);renderRows('large-'+symbol,large);
  const last=large[0],headline=$('headline-'+symbol);headline.textContent=last?(last.side==='buy'?'大额主动买入 ':'大额主动卖出 ')+number(last.amount)+' USDT':'等待大额成交';headline.className=last?.side||'muted';
 }
 $('last').textContent='后台已保存序号：'+s.cursor;
 $('logPath').textContent='日志：'+s.log_path;
 if(!first&&notify&&Notification.permission==='granted'){
  const fresh=s.large_trades.filter(t=>t.seq>cursor);
  if(fresh.length)new Notification('发现 '+fresh.length+' 笔大额成交',{body:fresh.slice(0,3).map(t=>t.symbol+' '+(t.side==='buy'?'买入':'卖出')+' '+number(t.amount)+' USDT').join('\n'),tag:'large-trades'});
 }
 cursor=Math.max(cursor,...s.large_trades.map(t=>t.seq));first=false;
}
$('settings').onsubmit=async event=>{
 event.preventDefault();saving=true;$('save').disabled=true;
 try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols:$('symbol').value,large_trade_usdt:Number($('threshold').value)}),signal:AbortSignal.timeout(10000)});const body=await r.json();if(!r.ok)throw Error(body.error||'保存失败');error();}catch(e){error(e.message);}finally{saving=false;$('save').disabled=false;}
};
$('notify').onclick=async()=>{if(!('Notification'in window)){error('浏览器不支持桌面通知');return;}notify=(await Notification.requestPermission())==='granted';$('notify').textContent=notify?'通知已开启':'通知未授权';};
async function poll(){try{if(!saving){const response=await fetch('/api/state',{signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('服务返回 '+response.status);const s=await response.json();if(!saving)render(s);}}catch(e){$('status').textContent='本地服务连接中断';$('status').className='';error(e.message+'；请确认后台程序仍在运行。');}finally{setTimeout(poll,1000);}}
poll();

let searchQuery=null, searchPage=1, searchPages=1, searching=false;
const chinaInput=stamp=>new Date(stamp+8*3600000).toISOString().slice(0,19);
$('searchEnd').value=chinaInput(Date.now());
$('searchStart').value=chinaInput(Date.now()-3600000);
async function loadHistorySymbols(){
 try{const r=await fetch('/api/symbols');if(!r.ok)throw Error('无法加载历史币种');const symbols=await r.json();
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
 $('searchMessage').textContent=query.symbol+' ｜ '+query.start.replace('T',' ')+' 至 '+query.end.replace('T',' ')+'（北京时间，结束时间不含） ｜ '+(query.kind==='large'?'仅大额成交':'全部成交')+' ｜ 共 '+number(result.total,0)+' 条';
 $('searchPage').textContent='第 '+searchPage+' / '+searchPages+' 页，每页 100 条';
 $('searchExport').href='/api/search.csv?'+new URLSearchParams(searchQuery);
 $('searchResults').hidden=false;
 }catch(e){$('searchMessage').textContent='查询失败：'+e.message;}
 finally{searching=false;$('searchButton').disabled=false;$('prevPage').disabled=searchPage<=1;$('nextPage').disabled=searchPage>=searchPages;}
}
$('searchForm').onsubmit=event=>{event.preventDefault();searchHistory(1,{symbol:$('searchSymbol').value,start:$('searchStart').value,end:$('searchEnd').value,kind:$('searchKind').value});};
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
