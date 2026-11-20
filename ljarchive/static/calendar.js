document.addEventListener('DOMContentLoaded', () => {
'use strict';
const source=window.CALENDAR;if(!source||!document.querySelector('.calendar-explorer'))return;
const $=id=>document.getElementById(id), months=['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
const shortMonths=['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'], weekdays=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
const weekdayNames=['Понедельники','Вторники','Среды','Четверги','Пятницы','Субботы','Воскресенья'];
const DAY=86400000,key='journal-archive-calendar:'+location.pathname;
const dateKey=date=>date.toISOString().slice(0,10), dateFrom=s=>new Date(s+'T12:00:00Z'), dateLabel=s=>dateFrom(s).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'UTC'});
const fmt=n=>n.toLocaleString('ru-RU'), sum=items=>items.reduce((n,p)=>n+p.comments,0);
const word=(n,forms)=>forms[n%10===1&&n%100!==11?0:n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?1:2];
const postsLabel=n=>`${fmt(n)} ${word(n,['запись','записи','записей'])}`, commentsLabel=n=>`${fmt(n)} ${word(n,['комментарий','комментария','комментариев'])}`;
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
function button(cls,text,attrs={}){const n=el('button',cls,text);n.type='button';for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n;}
let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}')||{};}catch{}
const state={year:null,view:['months','weeks','calendar'].includes(saved.view)?saved.view:'months',metric:saved.metric==='comments'?'comments':'posts',scale:saved.scale==='posts'?'posts':'time',compact:saved.compact!==false,selection:{type:'year'},limit:25};
let data=new Map(),days=[],posts=[],bounds=[],selectedPosts=[],commentPeak=1;
const issueLabel=p=>p.notice||(!p.complete&&!p.repost?'Полнота обсуждения не подтверждена':'');
const compactCount=n=>n<1000?fmt(n):`${(n/1000).toLocaleString('ru-RU',{maximumFractionDigits:1})}к`;
const postCommentLabel=p=>p.commentsExpected>p.comments&&!p.complete?`Комментарии: ${fmt(p.comments)} из ${fmt(p.commentsExpected)}`:commentsLabel(p.comments);
const summaryComments=items=>items.some(p=>!p.complete&&!p.repost)?`сохранено ${commentsLabel(sum(items))}`:commentsLabel(sum(items));
function makeDate(year,month=0,day=1){const d=new Date(0);d.setUTCFullYear(year,month,day);d.setUTCHours(0,0,0,0);return d;}
function validDate(value){if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return false;const d=dateFrom(value);return Number.isFinite(d.getTime())&&dateKey(d)===value;}
const records=(source.entries||[]).filter(p=>Number.isSafeInteger(p.id)&&p.id>0).map(p=>({id:p.id,date:String(p.date||'').slice(0,10),time:(String(p.date||'').match(/[ T]((?:[01]\d|2[0-3]):[0-5]\d)/)||[])[1]||'',title:String(p.title||'Без заголовка'),url:`entries/${p.id}.html`,comments:Math.max(0,Number(p.comments)||0),commentsExpected:p.repost?0:Math.max(0,Number(p.expected_comments)||0),complete:!!p.comments_complete,repost:!!p.repost,gap:p.state==='partial'||(p.state!=='ready'&&!p.repost),notice:String(p.notice||'')}));
const undated=records.filter(p=>!validDate(p.date));
const knownYears=[...new Set([...Object.keys(source.days||{}),...Object.keys(source.missing||{}),...records.map(p=>p.date)].filter(validDate).map(d=>Number(d.slice(0,4))))].sort((a,b)=>a-b);
const firstYear=knownYears[0],lastYear=knownYears.at(-1);
// Preserve intervening empty years. Unusual very wide spans are kept selectable without constructing thousands of empty years.
const years=knownYears.length?(lastYear-firstYear<=250?Array.from({length:lastYear-firstYear+1},(_,i)=>firstYear+i):knownYears):[];
for(const year of years)data.set(year,{posts:[],days:null});
for(const p of records)if(validDate(p.date))data.get(Number(p.date.slice(0,4)))?.posts.push(p);
for(const d of data.values())d.posts.sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time)||a.id-b.id);
function yearDays(year){const item=data.get(year);if(!item)return [];if(item.days)return item.days;const byDay=new Map();for(const p of item.posts){if(!byDay.has(p.date))byDay.set(p.date,[]);byDay.get(p.date).push(p);}const ds=[];for(let ms=+makeDate(year);ms<+makeDate(year+1);ms+=DAY){const d=new Date(ms),key=dateKey(d);ds.push({key,date:d,month:d.getUTCMonth(),weekday:(d.getUTCDay()+6)%7,posts:byDay.get(key)||[],missing:Math.max(0,Number(source.missing?.[key])||0)});}return item.days=ds;}
commentPeak=records.reduce((peak,p)=>Math.max(peak,p.comments),1);
state.year=years.includes(Number(saved.year))?Number(saved.year):lastYear;
function persist(){try{localStorage.setItem(key,JSON.stringify({year:state.year,view:state.view,metric:state.metric,scale:state.scale,compact:state.compact}));}catch{}}
function readHash(){const p=new URLSearchParams(location.hash.slice(1)||location.search.slice(1)),y=Number(p.get('year'));if(years.includes(y))state.year=y;if(['months','weeks','calendar'].includes(p.get('view')))state.view=p.get('view');if(['time','posts'].includes(p.get('scale')))state.scale=p.get('scale');}
function writeHash(){try{history.replaceState(null,'',`#year=${state.year}&view=${state.view}&scale=${state.scale}`);}catch{}persist();}
function metric(items){return state.metric==='posts'?items.length:sum(items);}
function computeBounds(){bounds=state.metric==='posts'?[0,1,2,3,5,8]:[0,1,5,20,80,300];}
function level(value){if(value===0)return 0;let result=1;for(let i=1;i<bounds.length;i++)if(value>=bounds[i])result=i;return result;}
function rateLevel(value){return value===0?0:value<.25?1:value<.75?2:value<1.5?3:value<3?4:5;}
function renderOverview(){
 const container=$('years');container.dataset.scale=state.scale;container.replaceChildren();
 const total=records.length-undated.length,totalDays=years.reduce((n,y)=>n+(makeDate(y+1)-makeDate(y))/DAY,0);
 const width=container.getBoundingClientRect().width;
 const options=[];
 for(const [year,d] of data){d.days=yearDays(year);
  const fraction=state.scale==='posts'?d.posts.length/Math.max(1,total):d.days.length/totalDays,yearWidth=fraction*width;
  const option=el('option','',`${year} · ${postsLabel(d.posts.length)}`);option.value=year;options.push(option);
  const b=button('year'+(yearWidth<34?' narrow':''),'',{'data-year':year,'data-total':d.posts.length,'aria-pressed':String(year===state.year),'aria-label':`${year}: ${postsLabel(d.posts.length)}. Открыть месяцы.`,title:`${year} · ${postsLabel(d.posts.length)}`});
  b.style.width=`${fraction*100}%`;
  if(fraction===0)b.hidden=true;
  const rates=months.map((_,m)=>{const ds=d.days.filter(day=>day.month===m);return ds.reduce((n,day)=>n+day.posts.length,0)/ds.length;});
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 1000 47');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');
  const rect=(attrs)=>{const r=document.createElementNS(svg.namespaceURI,'rect');for(const [k,v]of Object.entries(attrs))r.setAttribute(k,v);svg.append(r);return r;};
  let offset=0;
  months.forEach((_,m)=>{const ds=d.days.filter(day=>day.month===m),count=ds.reduce((n,day)=>n+day.posts.length,0);const span=state.scale==='posts'?count/Math.max(1,d.posts.length):ds.length/d.days.length;
   const patch=rect({x:offset*1000,y:40,width:span*1000,height:6,fill:`var(--heat${rateLevel(rates[m])})`});patch.dataset.month=m;patch.dataset.rate=rates[m].toFixed(5);offset+=span;
  });
  const binCount=Math.max(1,Math.min(500,Math.ceil(yearWidth/2))),bins=Array.from({length:binCount},()=>({comments:0,rate:0,n:0}));
  const yearStart=+makeDate(year),duration=+makeDate(year+1)-yearStart;
  d.posts.forEach((post,index)=>{const instant=Date.parse(post.date+'T'+(post.time||'12:00')+':00Z'),position=state.scale==='posts'?(index+.5)/d.posts.length:(instant-yearStart)/duration,bin=bins[Math.min(binCount-1,Math.floor(position*binCount))],m=Number(post.date.slice(5,7))-1;bin.comments=Math.max(bin.comments,post.comments);bin.rate+=rates[m];bin.n++;});
  bins.forEach((bin,i)=>{if(!bin.n)return;const height=4+Math.log1p(bin.comments)/Math.log1p(commentPeak)*30,step=1000/binCount;rect({x:i*step,y:37-height,width:Math.max(.1,step*.76),height,rx:Math.min(1,step*.1),fill:`var(--heat${rateLevel(bin.rate/bin.n)})`});});
  b.append(el('span','year-label',year),svg,el('small','num',fmt(d.posts.length)));container.append(b);
 }
 $('calendar-year').replaceChildren(...options);$('calendar-year').value=state.year;
 $('all-count').textContent=` · ${postsLabel(total)}`;
 document.querySelectorAll('button[data-scale]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.scale===state.scale));
 $('scale-caption').textContent=state.scale==='time'?'Равные интервалы времени · паузы видны':'Равная ширина на запись · паузы сжаты';
 const ranges=['0','До 0,25','0,25–0,75','0,75–1,5','1,5–3','3 и больше'];
 $('rate-legend').replaceChildren(...ranges.map((label,i)=>{const n=el('i','heat');n.dataset.level=i;n.title=label+' записи в день, в среднем за месяц';return n;}));
 const empty=state.scale==='posts'&&data.get(state.year).posts.length===0;
 $('overview-empty').hidden=!empty;$('overview-empty').textContent=`${state.year}: нет сохранённых записей. В масштабе по времени этот год виден на ленте.`;
}
function isSelected(d){const s=state.selection;return s.type==='undated'?false:s.type==='day'?d.key===s.key:s.type==='month'?d.month===s.month:s.type==='week'?d.key>=s.start&&d.key<=s.end:s.type==='weekday'?d.weekday===s.day:false;}
function dayCell(d,mode){
 const value=metric(d.posts),b=button('day heat'+(!d.posts.length?' empty':''),'',{'data-date':d.key,'data-level':level(value),'data-count':d.posts.length,'data-comments':sum(d.posts),'data-missing':d.missing,'data-gap':d.missing>0||d.posts.some(p=>p.gap),'aria-pressed':isSelected(d),'aria-label':`${dateLabel(d.key)}: ${postsLabel(d.posts.length)}, ${summaryComments(d.posts)}${d.missing?`; не получено записей: ${d.missing}`:d.posts.some(p=>p.gap)?'; есть пропуски':''}`});
 if(mode==='weeks')b.append(el('small','',d.date.getUTCDate()));
 b.append(el('span','',mode==='calendar'?d.date.getUTCDate():d.posts.length?compactCount(value):d.missing?'?':'·'));
 b.tabIndex=state.selection.type==='day'&&state.selection.key===d.key?0:-1;
 return b;
}
function tableShell(cls){const table=el('table','matrix '+cls);table.setAttribute('aria-label',cls==='months'?'Месяцы и дни месяца':'Недели и дни недели');const cols=el('colgroup');cols.append(el('col','label-col'));for(let i=0;i<(cls==='months'?31:7);i++)cols.append(el('col'));cols.append(el('col','total-col'));table.append(cols);const tr=el('tr');tr.append(el('th','',cls==='months'?'Месяц':'Неделя'));(cls==='months'?Array.from({length:31},(_,i)=>i+1):weekdays).forEach(x=>{const th=el('th','',x);th.scope='col';tr.append(th);});tr.append(el('th','','Σ'));const head=el('thead');head.append(tr);table.append(head);return table;}
function rowTotal(items,max,attrs){const v=metric(items),b=button('row-total','',attrs);b.style.setProperty('--size',(v/Math.max(1,max)*100)+'%');b.append(el('b','',fmt(v)));b.setAttribute('aria-label',`${v} ${state.metric==='posts'?'записей':'комментариев'}. Показать записи периода.`);return b;}
function renderMonths(){
 const table=tableShell('months'),body=el('tbody'),groups=months.map((_,m)=>days.filter(d=>d.month===m)),max=Math.max(...groups.map(group=>metric(group.flatMap(d=>d.posts))));
 groups.forEach((group,m)=>{const tr=el('tr'),th=el('th');th.scope='row';th.append(button('month-label',months[m],{'data-month':m,'aria-pressed':state.selection.type==='month'&&state.selection.month===m}));tr.append(th);
  for(let i=0;i<31;i++){const td=el('td');if(group[i])td.append(dayCell(group[i],'months'));else{const blank=el('div','outside');blank.setAttribute('aria-label','Такой даты нет');td.append(blank);}tr.append(td);}
  const td=el('td');td.append(rowTotal(group.flatMap(d=>d.posts),max,{'data-month':m}));tr.append(td);body.append(tr);
 });table.append(body);$('calendar-grid').replaceChildren(table);
}
function renderWeeks(){
 const table=tableShell('weeks'),body=el('tbody'),lookup=new Map(days.map(d=>[d.key,d]));table.classList.toggle('compact',state.compact);const start=days[0].date.getTime()-days[0].weekday*DAY,stop=days.at(-1).date.getTime(),groups=[];
 for(let ms=start;ms<=stop;ms+=7*DAY){const group=Array.from({length:7},(_,i)=>lookup.get(dateKey(new Date(ms+i*DAY)))||null);groups.push({ms,group,items:group.filter(Boolean).flatMap(d=>d.posts)});}
 const max=Math.max(...groups.map(g=>metric(g.items)));let previousMonth=-1;
 groups.forEach(({ms,group,items})=>{const from=new Date(ms),to=new Date(ms+6*DAY),month=group.find(Boolean).month,tr=el('tr',month!==previousMonth?'month-start':'');previousMonth=month;const th=el('th');th.scope='row';
  const range=`${from.getUTCDate()} ${shortMonths[from.getUTCMonth()]} — ${to.getUTCDate()} ${shortMonths[to.getUTCMonth()]}`;
  const attrs={'data-start':dateKey(from),'data-end':dateKey(to)};th.append(button('month-label',range,{...attrs,'aria-label':`${range}: показать неделю`,'aria-pressed':state.selection.type==='week'&&state.selection.start===dateKey(from)}));tr.append(th);
  group.forEach(d=>{const td=el('td');if(d)td.append(dayCell(d,'weeks'));else{const blank=el('div','outside');blank.setAttribute('aria-label','За пределами выбранного года');td.append(blank);}tr.append(td);});
  const td=el('td');td.append(rowTotal(items,max,attrs));tr.append(td);body.append(tr);
 });table.append(body);$('calendar-grid').replaceChildren(table);
 const values=weekdays.map((_,w)=>{const ds=days.filter(d=>d.weekday===w),total=metric(ds.flatMap(d=>d.posts));return {total,avg:total/ds.length};}),peak=Math.max(1,...values.map(x=>x.avg));
 $('week-summary').replaceChildren(el('p','week-caption','В среднем за день'), ...values.map((v,w)=>{const b=button('weekday','',{'data-weekday':w,'aria-pressed':state.selection.type==='weekday'&&state.selection.day===w,'aria-label':`${weekdayNames[w]}: всего ${v.total}, в среднем ${v.avg.toFixed(1)} за день. Показать записи.`});const bar=el('span','weekday-bar'),fill=el('i');fill.style.height=`${v.avg/peak*100}%`;bar.append(fill);b.append(el('span','weekday-name',weekdays[w]),bar,el('strong','num',v.avg.toLocaleString('ru-RU',{maximumFractionDigits:1})));return b;}));
}
function renderCalendar(){const grid=el('div','calendars');months.forEach((name,m)=>{const ds=days.filter(d=>d.month===m),section=el('section','mini-calendar'),head=el('header');head.append(button('month-label',name,{'data-month':m,'aria-pressed':state.selection.type==='month'&&state.selection.month===m}),el('span','num',fmt(metric(ds.flatMap(d=>d.posts)))));section.append(head);const cells=el('div','mini-days');weekdays.forEach(w=>cells.append(el('span','weekday-caption',w)));for(let i=0;i<ds[0].weekday;i++)cells.append(el('span'));ds.forEach(d=>cells.append(dayCell(d,'calendar')));section.append(cells);grid.append(section);});$('calendar-grid').replaceChildren(grid);}
function renderChart(){
 $('tooltip').hidden=true;computeBounds();$('week-summary').hidden=state.view!=='weeks';$('compact-option').hidden=state.view!=='weeks';$('compact').checked=state.compact;
 const labels={months:'Месяцы × дни месяца',weeks:'Недели × дни недели',calendar:'Календарь года'};
 $('chart-title').textContent=labels[state.view];
 $('chart-caption').textContent=state.view==='calendar'?'Число — дата. Цвет — '+(state.metric==='posts'?'записи.':'комментарии.'):'Ячейка — день. Число — '+(state.metric==='posts'?'записи.':'комментарии.');
 $('legend').replaceChildren(el('span','','0'));
 for(let i=0;i<=5;i++){const swatch=el('i','heat');swatch.dataset.level=i;const low=bounds[i],high=i===5?'':i===0?0:bounds[i+1]-1;swatch.title=i===0?'0':i===5?`${low}+`:`${low}–${high}`;$('legend').append(swatch);}
 $('legend').append(el('span','num',`${bounds.at(-1)}+`));
 if(state.view==='months')renderMonths();else if(state.view==='weeks')renderWeeks();else renderCalendar();
 const selected=$('calendar-grid').querySelector('.day[tabindex="0"]')||$('calendar-grid').querySelector('.day');if(selected)selected.tabIndex=0;
 document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.view===state.view));
 $('metric').value=state.metric;
}
function getSelection(){const s=state.selection;return s.type==='undated'?undated:s.type==='year'?posts:days.filter(isSelected).flatMap(d=>d.posts);}
function selectionName(){const s=state.selection;if(s.type==='undated')return 'Без даты';if(s.type==='month')return `${months[s.month]} ${state.year}`;if(s.type==='day')return dateLabel(s.key);if(s.type==='weekday')return `${weekdayNames[s.day]} · ${state.year}`;if(s.type==='week'){const a=dateFrom(s.start),b=dateFrom(s.end);return `${a.getUTCDate()} ${shortMonths[a.getUTCMonth()]} — ${b.getUTCDate()} ${shortMonths[b.getUTCMonth()]}`;}return String(state.year);}
function renderList(){
 const container=$('post-list');container.replaceChildren();
 if(!selectedPosts.length){container.append(el('p','empty-state',missingSelected()?'Записи пока не получены':'Нет сохранённых записей'));return;}
 selectedPosts.slice(0,state.limit).forEach(p=>{const row=el('article','post-item');row.dataset.post=p.id;const date=el('div','post-date');date.append(el('time','',validDate(p.date)?dateFrom(p.date).toLocaleDateString('ru-RU',{day:'numeric',month:'short',timeZone:'UTC'}):'Без даты'),el('span','num',p.time));const counts=el('div','post-count');const discussion=el('span',p.commentsExpected>p.comments&&!p.complete?'post-gap':'',postCommentLabel(p));discussion.title=p.notice;counts.append(discussion);if(issueLabel(p)){const status=el('span','post-gap',issueLabel(p));status.title=p.notice;counts.append(status);}row.append(date,postLink(p,'post-open',p.title),counts);container.append(row);});
 if(selectedPosts.length>state.limit)container.append(button('more-posts',`Ещё записи · ${selectedPosts.length-state.limit}`,{'data-more':''}));
}
function renderInspector(){
 selectedPosts=getSelection();$('selection-title').textContent=selectionName();$('inspector-eyebrow').textContent={undated:'Записи',year:'Год',month:'Месяц',day:'День',week:'Неделя',weekday:'День недели'}[state.selection.type];
 $('selection-counts').textContent=`${postsLabel(selectedPosts.length)} · ${summaryComments(selectedPosts)}`;$('clear-selection').hidden=state.selection.type==='year';
 const comb=$('micro-comb');comb.replaceChildren();comb.style.overflowX='auto';comb.style.height='49px';
 selectedPosts.forEach(p=>{const b=postLink(p,'','',{'data-open':p.id,'data-gap':p.gap,'data-comments':p.comments,'data-expected':p.commentsExpected,'aria-label':`${validDate(p.date)?dateLabel(p.date):'Без даты'} · ${p.title} · ${postCommentLabel(p)}`,'title':`${p.date} · ${p.title} · ${postCommentLabel(p)}${issueLabel(p)?' · '+issueLabel(p):''}`});b.style.flex='0 0 5px';b.style.height=`${5+Math.log1p(p.comments)/Math.log1p(commentPeak)*29}px`;comb.append(b);});comb.hidden=!selectedPosts.length;
 $('selection-note').textContent=missingSelected()?`Не получено записей: ${missingSelected()}`:selectedPosts.length?'Высота — сохранённые комментарии.':'По сохранённым данным.';
const feed=$('period-feed'),s=state.selection;feed.hidden=!selectedPosts.length||['week','weekday','undated'].includes(s.type);feed.href='index.html?'+new URLSearchParams({view:'full',...(s.type==='day'?{date:s.key}:s.type==='month'?{month:`${state.year}-${String(s.month+1).padStart(2,'0')}`}:{year:state.year})});
 renderList();$('post-list').scrollTop=0;
 $('mobile-jump').hidden=state.selection.type==='year';$('mobile-jump').textContent=`${postsLabel(selectedPosts.length)} ↓`;
 $('announcement').textContent=`${selectionName()}. ${postsLabel(selectedPosts.length)}. ${commentsLabel(sum(selectedPosts))}.`;
}
function select(selection){state.selection=selection;state.limit=25;if(years.length)renderChart();renderInspector();}
function chooseYear(year,view=state.view){if(!years.includes(year))return;state.year=year;state.view=view;state.selection={type:'year'};state.limit=25;render();centerYear();}
function render(){const d=data.get(state.year);days=yearDays(state.year);posts=d?.posts||[];$('year-title').textContent=state.year||'—';$('calendar-count').replaceChildren(el('b','',postsLabel(posts.length)),document.createTextNode(' · '+summaryComments(posts)));$('calendar-prev').disabled=years.indexOf(state.year)<=0;$('calendar-next').disabled=years.indexOf(state.year)>=years.length-1;renderOverview();renderChart();renderInspector();writeHash();}
function postLink(p,cls,text,attrs={}){const a=el('a',cls,text);a.href=p.url;for(const [k,v]of Object.entries(attrs))a.setAttribute(k,v);return a;}
function missingSelected(){return state.selection.type==='undated'?0:days.filter(d=>state.selection.type==='year'||isSelected(d)).reduce((n,d)=>n+d.missing,0);}
function tooltip(target){const d=days.find(x=>x.key===target.dataset.date);if(!d)return;const tip=$('tooltip');tip.replaceChildren(el('strong','',dateLabel(d.key)),el('span','',`${postsLabel(d.posts.length)} · ${summaryComments(d.posts)}`));d.posts.slice(0,2).forEach(p=>tip.append(el('small','',p.title)));d.posts.filter(p=>p.gap).slice(0,2).forEach(p=>tip.append(el('small','post-gap',issueLabel(p))));if(d.missing)tip.append(el('small','post-gap',`Не получено записей: ${d.missing}`));tip.hidden=false;const rect=target.getBoundingClientRect(),size=tip.getBoundingClientRect();tip.style.left=Math.max(8,Math.min(innerWidth-size.width-8,rect.left+rect.width/2-size.width/2))+'px';tip.style.top=Math.max(8,rect.top-size.height-8)+'px';}
document.addEventListener('click',event=>{
 const b=event.target.closest('button');if(!b)return;
 if(b.dataset.scale){state.scale=b.dataset.scale;if(years.length){renderOverview();centerYear();}writeHash();return;}
 if(b.dataset.year){chooseYear(Number(b.dataset.year),'months');return;}
 if(b.dataset.view){state.view=b.dataset.view;if(years.length)renderChart();writeHash();return;}
 if(b.dataset.date){const key=b.dataset.date;select({type:'day',key});$('calendar-grid').querySelector(`[data-date="${key}"]`)?.focus({preventScroll:true});return;}
 if(b.dataset.month!==undefined){const m=Number(b.dataset.month);select({type:'month',month:m});$('calendar-grid').querySelector(`.month-label[data-month="${m}"]`)?.focus({preventScroll:true});return;}
 if(b.dataset.start){const start=b.dataset.start;select({type:'week',start,end:b.dataset.end});$('calendar-grid').querySelector(`.month-label[data-start="${start}"]`)?.focus({preventScroll:true});return;}
 if(b.dataset.weekday!==undefined){const day=Number(b.dataset.weekday);select({type:'weekday',day});$('week-summary').querySelector(`[data-weekday="${day}"]`)?.focus({preventScroll:true});return;}

 if(b.hasAttribute('data-more')){const scroll=$('post-list').scrollTop;state.limit+=25;renderList();$('post-list').scrollTop=scroll;return;}

});
$('calendar-prev').onclick=()=>chooseYear(years[years.indexOf(state.year)-1]);$('calendar-next').onclick=()=>chooseYear(years[years.indexOf(state.year)+1]);
$('clear-selection').onclick=()=>select({type:'year'});
$('calendar-year').onchange=e=>chooseYear(Number(e.target.value),'months');
$('calendar-undated').onclick=()=>select({type:'undated'});
$('compact').onchange=e=>{state.compact=e.target.checked;renderChart();persist();};
$('metric').onchange=e=>{state.metric=e.target.value;if(years.length)renderChart();writeHash();};
$('mobile-jump').onclick=()=>$('inspector').scrollIntoView({block:'start',behavior:'auto'});
$('calendar-grid').addEventListener('pointerover',e=>{if(e.pointerType==='touch')return;const b=e.target.closest('[data-date]');if(b)tooltip(b);});
$('calendar-grid').addEventListener('pointerout',()=>$('tooltip').hidden=true);
$('calendar-grid').addEventListener('focusin',e=>{const b=e.target.closest('[data-date]');if(b)tooltip(b);});
$('calendar-grid').addEventListener('focusout',()=>$('tooltip').hidden=true);
window.addEventListener('scroll',()=>$('tooltip').hidden=true,true);window.addEventListener('resize',()=>$('tooltip').hidden=true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('tooltip').hidden=true;}const b=e.target.closest('[data-date]');if(!b||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;e.preventDefault();const old=days.find(d=>d.key===b.dataset.date);let d=new Date(old.date);
 if(e.key==='ArrowLeft'||e.key==='ArrowRight')d.setUTCDate(d.getUTCDate()+(e.key==='ArrowLeft'?-1:1));
 if(e.key==='ArrowUp'||e.key==='ArrowDown'){const delta=e.key==='ArrowUp'?-1:1;if(state.view==='months'){const day=d.getUTCDate();d.setUTCDate(1);d.setUTCMonth(d.getUTCMonth()+delta);const max=makeDate(d.getUTCFullYear(),d.getUTCMonth()+1,0).getUTCDate();d.setUTCDate(Math.min(day,max));}else d.setUTCDate(d.getUTCDate()+delta*7);}
 if(e.key==='Home')d.setUTCDate(1);if(e.key==='End')d=makeDate(state.year,d.getUTCMonth()+1,0);
 const next=$('calendar-grid').querySelector(`[data-date="${dateKey(d)}"]`);if(next){$('calendar-grid').querySelectorAll('[data-date]').forEach(x=>x.tabIndex=-1);next.tabIndex=0;next.focus();}
});
window.addEventListener('hashchange',()=>{readHash();state.selection={type:'year'};if(years.length)render();});
$('inspector').before($('mobile-jump'));
function centerYear(){const b=$('years').querySelector(`[data-year="${state.year}"]`),sc=$('years').parentElement;if(b&&sc.scrollWidth>sc.clientWidth){const br=b.getBoundingClientRect(),sr=sc.getBoundingClientRect();sc.scrollLeft+=br.left-sr.left-sc.clientWidth/2+b.clientWidth/2;}}
let overviewResize;window.addEventListener('resize',()=>{clearTimeout(overviewResize);overviewResize=setTimeout(()=>{if(years.length){renderOverview();centerYear();}},100);});
$('overview-range').textContent=years.length?`${firstYear} — ${lastYear}`:'Нет записей';
$('calendar-undated').hidden=!undated.length;$('calendar-undated').textContent=`Без даты: ${undated.length}`;
$('calendar-coverage').textContent=source.partial||!source.discovery_complete?'По сохранённым данным · состояние архива — в отчёте':'';
if(!years.length){$('calendar-year').disabled=true;$('calendar-prev').disabled=true;$('calendar-next').disabled=true;$('calendar-grid').replaceChildren(el('p','calendar-empty','Записей с датами пока нет'));state.selection={type:'undated'};renderInspector();return;}
readHash();render();centerYear();
});
