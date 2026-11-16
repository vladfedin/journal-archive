'use strict';
// Constant-width slots, virtualized DOM: a twenty-year journal does not create thousands of links at once.
window.ArchiveTimeline=class {
  constructor(root,base=''){
    this.root=root;root.setAttribute('aria-live','off');this.base=base;this.items=[];this.slot=12;this.active=null;
    const node=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls;if(text)n.textContent=text;return n;};
    this.head=node('div','timeline-head');this.count=node('span','muted small');this.year=node('select','input timeline-year');this.year.setAttribute('aria-label','Год');
    this.current=node('button','btn text','К текущей');this.current.type='button';this.current.onclick=()=>this.jump(this.items.findIndex(i=>i.id===this.active));
    this.gap=node('button','btn text','Пропуски');this.gap.type='button';this.gap.setAttribute('aria-label','Следующая запись с пропусками');this.gap.onclick=()=>{const after=Math.floor(this.viewport.scrollLeft/this.slot)+1;let next=this.items.findIndex((i,n)=>n>=after&&i.state==='partial');if(next<0)next=this.items.findIndex(i=>i.state==='partial');this.jump(next,false);this.track.querySelector(`[data-timeline-index="${next}"]`)?.focus({preventScroll:true});};
    this.head.append(this.count,this.year,this.gap,this.current);this.year.onchange=()=>this.jump(this.items.findIndex(i=>i.date.startsWith(this.year.value)),false);
    this.overview=node('canvas','timeline-overview');this.overview.setAttribute('aria-hidden','true');
    this.overview.onclick=e=>this.jump(Math.floor((e.clientX-this.overview.getBoundingClientRect().left)/this.overview.clientWidth*this.items.length));
    this.viewport=node('div','timeline-viewport');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Записи по времени. Прокрутите вправо или выберите год.');
    this.track=node('div','timeline-track');this.viewport.append(this.track);
    this.tip=node('div','timeline-tip');this.tip.hidden=true;this.tip.setAttribute('role','status');
    const legend=node('div','timeline-legend');for(const [cls,text] of [['pending','Ожидают'],['loading','Загружаются'],['ready','Сохранены'],['partial','Есть пропуски']]){legend.append(node('span',cls,text));}
    root.replaceChildren(this.head,this.overview,this.viewport,this.tip,legend);this.viewport.onscroll=()=>this.draw();
    this.viewport.onkeydown=e=>{if(e.target!==this.viewport)return;if(['ArrowRight','ArrowLeft','Home','End'].includes(e.key)){e.preventDefault();this.viewport.scrollLeft=e.key==='Home'?0:e.key==='End'?this.track.offsetWidth:this.viewport.scrollLeft+(e.key==='ArrowRight'?120:-120);}};
    this.resize=new ResizeObserver(()=>this.draw());this.resize.observe(this.viewport);
  }
  jump(index,center=true){if(index>=0){this.viewport.scrollLeft=Math.max(0,index*this.slot-(center?this.viewport.clientWidth/2:0));this.draw();}}
  set(data,active=null){
    this.data=data;this.active=active;const rows=[...data.entries];
    // Calendar placeholders are generated as a range, with no invented entry IDs or links.
    for(const gap of data.gaps||[])rows.push({date:gap.date,count:gap.count,state:'pending'});
    rows.sort((a,b)=>a.date.localeCompare(b.date)||(a.id||0)-(b.id||0));
    this.items=[];for(const row of rows){if(row.id)this.items.push(row);else for(let n=0;n<row.count;n++)this.items.push({...row,count:undefined});}
    this.count.textContent=`${data.ready} / ${data.expected} записей${data.partial?' · с пропусками: '+data.partial:''}`;
    const years=[...new Set(this.items.map(i=>i.date.slice(0,4)).filter(Boolean))];const value=this.year.value;
    if(years.join(',')!==this.years){this.years=years.join(',');this.year.replaceChildren(...years.map(y=>new Option(y,y)));if(years.includes(value))this.year.value=value;}
    this.gap.hidden=!data.partial;this.current.hidden=!active;this.track.style.width=Math.max(1,this.items.length*this.slot)+'px';this.draw();
  }
  description(item){
    if(!item.id)return `${item.date} · запись ещё не получена`;
    const m=item.materials||{}, who=(item.top||[]).map(([name,n])=>`${name}: ${n}`).join(', ');
    return `${item.date} · ${item.title}\nКомментарии: ${item.comments}${item.expected_comments!=null?' / '+item.expected_comments:''}${item.pages!=null?' · страницы: '+item.page+' / '+item.pages:''}\nМатериалы: ${m.saved||0} / ${m.requested??'?'}${who?'\nЧаще комментировали: '+who:''}${item.error?'\n'+item.error:''}`;
  }
  draw(){
    const start=Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)-4),end=Math.min(this.items.length,Math.ceil((this.viewport.scrollLeft+this.viewport.clientWidth)/this.slot)+4);
    const focused=document.activeElement?.dataset?.timelineIndex;const nodes=[];
    for(let n=start;n<end;n++){
      const item=this.items[n],m=item.materials||{},total=Math.max(item.expected_comments||0,item.comments||0)+(m.requested||0)+1;
      const done=1+(item.comments||0)+(m.saved||0),bar=document.createElement(item.id?'a':'span');bar.className='timeline-bar '+item.state+(item.id===this.active?' active':'');
      bar.style.left=(n*this.slot)+'px';bar.style.height=(item.id?Math.min(116,18+Math.log2(total)*10):14)+'px';
      bar.dataset.timelineIndex=String(n);if(item.id)bar.href=this.base+item.url;
      const description=this.description(item);bar.title=description;bar.setAttribute('aria-label',description);
      const fill=document.createElement('i');fill.style.height=(item.state==='ready'?100:Math.min(94,100*done/(total+1)))+'%';bar.append(fill);
      const show=()=>{this.tip.textContent=description;this.tip.hidden=false;};bar.onmouseenter=show;bar.onfocus=show;
      bar.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const target=Math.max(0,Math.min(this.items.length-1,e.key==='Home'?0:e.key==='End'?this.items.length-1:n+(e.key==='ArrowRight'?1:-1)));this.jump(target);this.track.querySelector(`[data-timeline-index="${target}"]`)?.focus({preventScroll:true});}};
      nodes.push(bar);
      if(n===0||this.items[n-1].date.slice(0,4)!==item.date.slice(0,4)){const label=document.createElement('span');label.className='timeline-year-mark';label.style.left=(n*this.slot)+'px';label.textContent=item.date.slice(0,4);nodes.push(label);}
    }
    const visible=Math.min(this.items.length-1,Math.floor(this.viewport.scrollLeft/this.slot));
    if(this.items[visible]){const label=document.createElement('span');label.className='timeline-year-mark';label.style.left=(this.viewport.scrollLeft+2)+'px';label.textContent=this.items[visible].date.slice(0,4);nodes.push(label);}
    this.track.replaceChildren(...nodes);if(focused!==undefined)this.track.querySelector(`[data-timeline-index="${focused}"]`)?.focus({preventScroll:true});this.drawOverview();
  }
  drawOverview(){
    const width=Math.round(this.viewport.clientWidth),height=32;if(!width)return;this.overview.width=width;this.overview.height=height;
    const ctx=this.overview.getContext('2d'),n=this.items.length;if(!n)return;
    for(let x=0;x<width;x++){const from=Math.min(n-1,Math.floor(x*n/width)),to=Math.max(from+1,Math.floor((x+1)*n/width));let state='ready',h=4;const priority={ready:0,pending:1,loading:2,partial:3};for(let i=from;i<to;i++){const item=this.items[i];if(priority[item.state]>priority[state])state=item.state;h=Math.max(h,item.id?Math.min(27,5+Math.log2(1+(item.expected_comments||item.comments||0))*2.2):4);}ctx.fillStyle={ready:'#6c917b',partial:'#c59254',loading:'#688da3',pending:'#b9bbb580'}[state];ctx.fillRect(x,height-h,1,h);}
    const left=this.viewport.scrollLeft/(n*this.slot)*width,span=Math.min(width,this.viewport.clientWidth/(n*this.slot)*width);ctx.strokeStyle='#6688a1';ctx.lineWidth=2;ctx.strokeRect(left+1,1,Math.max(3,span-2),height-2);
  }
};
if(document.getElementById('archive-timeline')&&window.TIMELINE)new window.ArchiveTimeline(document.getElementById('archive-timeline')).set(window.TIMELINE);

if(document.getElementById('archive-timeline')?.dataset.source){
  const root=document.getElementById('archive-timeline'),view=new window.ArchiveTimeline(root);
  const refresh=async()=>{try{const r=await fetch(root.dataset.source,{cache:'no-store'});if(r.ok){const data=await r.json();view.set(data,data.active);}}catch(e){view.count.textContent='Нет соединения · показаны сохранённые данные';}finally{setTimeout(refresh,8000);}};
  refresh();
}
