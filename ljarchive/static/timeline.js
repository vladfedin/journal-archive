'use strict';
// Equal calendar years above; individual, virtualized entries below.
window.ArchiveTimeline=class {
  constructor(root,base=''){
    root.timelineView?.destroy();root.timelineView=this;
    this.root=root;this.base=base;this.items=[];this.active=null;this.slot=12;this.bands=[];
    this.node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
    const node=this.node,button=(text,label,action)=>{const b=node('button','btn text',text);b.type='button';b.setAttribute('aria-label',label);b.onclick=action;return b;};
    root.setAttribute('aria-live','off');this.head=node('div','timeline-head');this.count=node('span','muted small');
    this.year=node('select','input timeline-year');this.year.setAttribute('aria-label','Год');
    this.current=button('К текущей','К загружаемой записи',()=>this.jump(this.items.findIndex(i=>i.id===this.active)));
    this.gap=button('Пропуски','Следующая запись с пропусками',()=>{const after=(this.lastGap??Math.floor(this.viewport.scrollLeft/this.slot))+1;let next=this.items.findIndex((i,n)=>n>=after&&i.state==='partial');if(next<0)next=this.items.findIndex(i=>i.state==='partial');this.lastGap=next;this.jump(next);this.track.querySelector(`[data-timeline-index="${next}"]`)?.focus({preventScroll:true});});
    this.head.append(this.count,this.year,this.gap,this.current);
    this.year.onchange=()=>{const band=this.bands.find(b=>b.label===this.year.value);if(band?.count)this.jump(band.start,false);};
    this.overview=node('canvas','timeline-overview');this.overview.setAttribute('aria-hidden','true');this.cache=document.createElement('canvas');
    this.caption=node('div','timeline-caption');this.caption.append(node('span','','Один участок — год'));this.windowLabel=node('span');this.caption.append(this.windowLabel);
    this.detailHead=node('div','timeline-detail-head');const range=node('div');this.range=node('strong');this.rangeCount=node('span','muted small');range.append(this.range,this.rangeCount);
    const nav=node('div','timeline-nav');this.prev=button('←','Предыдущие записи',()=>this.shift(-1));this.next=button('→','Следующие записи',()=>this.shift(1));
    const help=node('details','timeline-help'),summary=node('summary','','?');summary.setAttribute('aria-label','Как читать хронологию');
    help.append(summary,node('p','','Высота — комментарии и материалы, по логарифмической шкале. Цвет — состояние загрузки. На общей карте несколько записей могут занимать один пиксель; пропуски остаются видны.'));nav.append(this.prev,this.next,help);this.detailHead.append(range,nav);
    this.viewport=node('div','timeline-viewport');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Записи по времени. Стрелки — перемещение, Home и End — начало и конец.');
    this.track=node('div','timeline-track');this.viewport.append(this.track);this.tip=node('div','timeline-tip');this.tip.hidden=true;this.tip.setAttribute('role','status');
    const legend=node('div','timeline-legend');for(const [cls,text] of [['ready','Сохранены'],['loading','Загружаются'],['partial','Есть пропуски'],['repost','Перепосты'],['pending','Ожидают']])legend.append(node('span',cls,text));
    root.replaceChildren(this.head,this.overview,this.caption,this.detailHead,this.viewport,legend,this.tip);this.viewport.onscroll=()=>this.schedule();
    this.viewport.onkeydown=e=>{if(e.target!==this.viewport)return;if(['ArrowRight','ArrowLeft','Home','End'].includes(e.key)){e.preventDefault();this.jump(e.key==='Home'?0:e.key==='End'?this.items.length-1:Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)+(e.key==='ArrowRight'?10:-10)),false);}};
    this.overview.onpointerdown=e=>{if(e.button!==0||!this.items.length)return;const box=this.overview.getBoundingClientRect(),ratio=(e.clientX-box.left)/box.width,band=this.bandAt(ratio);if(e.clientY-box.top<23){if(band?.count)this.jump(band.start,false);return;}const index=this.indexAt(ratio),start=this.viewport.scrollLeft/this.slot,visible=this.viewport.clientWidth/this.slot;this.dragOffset=index>=start&&index<start+visible?index-start:visible/2;this.drag=true;this.overview.setPointerCapture(e.pointerId);this.jump(Math.max(0,index-this.dragOffset),false);};
    this.overview.onpointermove=e=>{const box=this.overview.getBoundingClientRect(),ratio=(e.clientX-box.left)/box.width,band=this.bandAt(ratio);if(band)this.overview.title=`${band.label} · ${band.count} записей${band.partial?' · с пропусками: '+band.partial:''}`;if(this.drag)this.jump(Math.max(0,this.indexAt(ratio)-this.dragOffset),false);};
    const release=e=>{this.drag=false;if(this.overview.hasPointerCapture(e.pointerId))this.overview.releasePointerCapture(e.pointerId);};this.overview.onpointerup=release;this.overview.onpointercancel=release;
    this.resize=new ResizeObserver(()=>{const old=this.slot;this.slot=this.viewport.clientWidth<560||matchMedia('(pointer: coarse)').matches?44:12;if(old!==this.slot){this.track.style.width=Math.max(1,this.items.length*this.slot)+'px';this.viewport.scrollLeft=this.viewport.scrollLeft/old*this.slot;}this.invalidate();});this.resize.observe(this.viewport);
    this.theme=new MutationObserver(()=>this.invalidate());this.theme.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  }
  destroy(){this.resize?.disconnect();this.theme?.disconnect();cancelAnimationFrame(this.frame);}
  schedule(){if(this.frame)return;this.frame=requestAnimationFrame(()=>{this.frame=null;this.draw();});}
  invalidate(){this.cacheKey=null;this.drawKey=null;this.draw();}
  shift(direction){this.jump(Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)+direction*Math.max(1,Math.floor(this.viewport.clientWidth/this.slot*.8))),false);}
  jump(index,center=true){if(index>=0&&this.items.length){this.viewport.scrollLeft=Math.max(0,index*this.slot-(center?this.viewport.clientWidth/2:0));this.draw();}}
  set(data,active=null){
    const anchor=this.items[Math.floor(this.viewport.scrollLeft/this.slot)],offset=this.viewport.scrollLeft%this.slot;
    this.data=data;this.active=active;const rows=[...data.entries];for(const gap of data.gaps||[])rows.push({date:gap.date,count:gap.count,state:'pending'});
    rows.sort((a,b)=>(a.date||'').localeCompare(b.date||'')||(a.id||0)-(b.id||0));this.items=[];for(const row of rows){if(row.id)this.items.push(row);else for(let n=0;n<row.count;n++)this.items.push({...row,count:undefined});}
    const known=this.items.filter(i=>/^\d{4}-/.test(i.date||'')),unknown=this.items.filter(i=>!/^\d{4}-/.test(i.date||''));this.items=[...known,...unknown];
    const groups=new Map();this.items.forEach((item,index)=>{const label=/^\d{4}-/.test(item.date||'')?item.date.slice(0,4):'Без даты';if(!groups.has(label))groups.set(label,{label,start:index,count:0,partial:0});const g=groups.get(label);g.count++;if(item.state==='partial')g.partial++;});
    this.bands=[];if(known.length){for(let year=Number(known[0].date.slice(0,4));year<=Number(known.at(-1).date.slice(0,4));year++){const label=String(year);this.bands.push(groups.get(label)||{label,start:0,count:0,partial:0});}}if(unknown.length)this.bands.push(groups.get('Без даты'));
    this.bandForItem=new Int32Array(this.items.length);this.bands.forEach((b,index)=>{for(let i=b.start;i<b.start+b.count;i++)this.bandForItem[i]=index;});
    this.count.textContent=`${data.ready} / ${data.expected} записей${data.partial?' · с пропусками: '+data.partial:''}`;
    const value=this.year.value;this.year.replaceChildren(...this.bands.map(b=>{const o=new Option(b.label+(b.count?'':' · 0'),b.label);o.disabled=!b.count;return o;}));if(this.bands.some(b=>b.label===value))this.year.value=value;
    this.gap.hidden=!data.partial;this.current.hidden=!active;this.track.style.width=Math.max(1,this.items.length*this.slot)+'px';if(anchor?.id){const index=this.items.findIndex(i=>i.id===anchor.id);if(index>=0)this.viewport.scrollLeft=index*this.slot+offset;}this.invalidate();
  }
  bandAt(ratio){return this.bands[Math.min(this.bands.length-1,Math.max(0,Math.floor(ratio*this.bands.length)))];}
  indexAt(ratio){if(!this.bands.length)return 0;const pos=Math.max(0,Math.min(.999999,ratio))*this.bands.length,b=Math.floor(pos),band=this.bands[b];if(band.count)return band.start+Math.floor((pos-b)*band.count);return this.bands.slice(b+1).find(y=>y.count)?.start??Math.max(0,this.items.length-1);}
  position(index,end=false){if(!this.items.length)return 0;const i=Math.max(0,Math.min(this.items.length-1,Math.floor(index-(end?1:0)))),b=this.bandForItem[i],band=this.bands[b];return (b+(i-band.start+(end?1:0))/band.count)/this.bands.length;}
  description(item){
    if(!item.id)return `${item.date} · запись ещё не получена`;
    const m=item.materials||{},who=(item.top||[]).map(([name,n])=>`${name}: ${n}`).join(', '),materials=`Материалы: ${m.saved||0} / ${m.requested??'?'}`;
    if(item.repost)return `${item.date} · ${item.title} · Перепост\n${materials}${item.notice?'\n'+item.notice:''}`;
    const complete=item.comments_complete===true,comments=`Комментарии: ${item.comments}${complete?' · сохранены':item.expected_comments!=null?' / '+item.expected_comments:''}${item.screened?' · скрытых: '+item.screened:''}`;
    return `${item.date} · ${item.title}\n${comments}${!complete&&item.pages!=null?' · страницы: '+item.page+' / '+item.pages:''}\n${materials}${item.notice?'\n'+item.notice:''}${who?'\nЧаще комментировали: '+who:''}${item.error?'\n'+item.error:''}`;
  }
  draw(){
    const width=this.viewport.clientWidth;if(!width)return;
    const visibleStart=Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)),visibleEnd=Math.min(this.items.length,Math.ceil((this.viewport.scrollLeft+width)/this.slot)),start=Math.max(0,visibleStart-2),end=Math.min(this.items.length,visibleEnd+2),key=[start,end,this.active,this.slot,width].join(':');
    if(this.drawKey!==key){
      this.drawKey=key;const focused=this.track.contains(document.activeElement)?document.activeElement.dataset.timelineIndex:undefined,nodes=[];
      for(const band of this.bands){if(!band.count||band.start>=end||band.start+band.count<=start)continue;const from=Math.max(start,band.start),to=Math.min(end,band.start+band.count),shade=this.node('span','timeline-year-band');shade.style.left=from*this.slot+'px';shade.style.width=(to-from)*this.slot+'px';if(Number(band.label)%2===0)shade.classList.add('even');nodes.push(shade);const x=Math.max(this.viewport.scrollLeft+4,band.start*this.slot+4),space=Math.min(this.viewport.scrollLeft+width,(band.start+band.count)*this.slot)-x;if(space>37){const label=this.node('span','timeline-year-mark',band.label);label.style.left=x+'px';nodes.push(label);}}
      let lastMonth='',lastTick=-100;
      for(let n=start;n<end;n++){
        const item=this.items[n],m=item.materials||{},total=Math.max(item.expected_comments||0,item.comments||0)+(m.requested||0)+1,done=1+(item.comments||0)+(m.saved||0);
        const bar=this.node(item.id?'a':'span','timeline-bar '+item.state+(item.repost?' repost':'')+(item.id===this.active?' active':''));bar.style.left=n*this.slot+'px';bar.style.width=this.slot+'px';bar.dataset.timelineIndex=String(n);if(item.id)bar.href=this.base+item.url;
        const description=this.description(item);bar.title=description;bar.setAttribute('aria-label',description);
        const track=this.node('span','timeline-column');track.style.height=(item.id?Math.min(116,18+Math.log2(total)*10):14)+'px';const fill=document.createElement('i');fill.style.height=(item.state==='ready'?100:Math.min(94,100*done/(total+1)))+'%';track.append(fill);bar.append(track);
        const show=()=>{this.tip.textContent=description;this.tip.hidden=false;};bar.onmouseenter=show;bar.onfocus=show;
        bar.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();let target=Math.max(0,Math.min(this.items.length-1,e.key==='Home'?0:e.key==='End'?this.items.length-1:n+(e.key==='ArrowRight'?1:-1)));const direction=e.key==='ArrowLeft'||e.key==='End'?-1:1;while(this.items[target]&&!this.items[target].id)target+=direction;this.jump(target);this.track.querySelector(`[data-timeline-index="${target}"]`)?.focus({preventScroll:true});}};nodes.push(bar);
        const month=(item.date||'').slice(0,7),x=n*this.slot;if(month!==lastMonth&&x>=this.viewport.scrollLeft&&x-lastTick>65&&x<this.viewport.scrollLeft+width-25){const text=['янв.','февр.','март','апр.','май','июнь','июль','авг.','сент.','окт.','нояб.','дек.'][Number(month.slice(5))-1];if(text){const tick=this.node('span','timeline-month',text);tick.style.left=x+3+'px';nodes.push(tick);lastTick=x;}}lastMonth=month;
      }
      this.track.replaceChildren(...nodes);if(focused!==undefined)this.track.querySelector(`[data-timeline-index="${focused}"]`)?.focus({preventScroll:true});
    }
    const first=this.items[visibleStart],last=this.items[visibleEnd-1];if(first&&last){const a=(first.date||'').slice(0,4)||'Без даты',b=(last.date||'').slice(0,4)||'Без даты';this.range.textContent=a===b?a:`${a} — ${b}`;this.rangeCount.textContent=`${visibleEnd-visibleStart} записей · ${(first.date||'').slice(0,10)} — ${(last.date||'').slice(0,10)}`;this.windowLabel.textContent=this.range.textContent;this.year.value=a;}else{this.range.textContent='Записи ещё не получены';this.rangeCount.textContent='';this.windowLabel.textContent='';}
    this.prev.disabled=visibleStart===0;this.next.disabled=visibleEnd>=this.items.length;this.drawOverview(visibleStart,visibleEnd);
  }
  drawOverview(start,end){
    const width=Math.round(this.viewport.clientWidth),height=84,dpr=window.devicePixelRatio||1,key=[width,dpr].join(':');if(!width)return;
    if(this.cacheKey!==key){
      this.cacheKey=key;this.cache.width=Math.round(width*dpr);this.cache.height=Math.round(height*dpr);const ctx=this.cache.getContext('2d');ctx.scale(dpr,dpr);
      const style=getComputedStyle(this.root),color=name=>style.getPropertyValue('--timeline-'+name).trim();this.palette={ready:color('ready'),pending:color('pending'),loading:color('loading'),partial:color('partial'),repost:color('repost'),window:color('window')};
      let lastLabel=-100;ctx.font='11px '+style.fontFamily;ctx.textAlign='center';
      this.bands.forEach((band,n)=>{const x=n*width/this.bands.length,w=width/this.bands.length;if(n%2===0){ctx.fillStyle=color('band');ctx.fillRect(x,0,w,74);}ctx.fillStyle=style.getPropertyValue('--line');ctx.fillRect(Math.floor(x),0,1,74);const labelWidth=ctx.measureText(band.label).width,center=Math.max(labelWidth/2+1,Math.min(width-labelWidth/2-1,x+w/2));if(center-labelWidth/2>lastLabel+5){ctx.fillStyle=style.color;ctx.fillText(band.label,center,15);lastLabel=center+labelWidth/2;}});
      // One bin per CSS pixel; a gap wins over completed records sharing that pixel.
      const bins=Array.from({length:width},()=>({state:'ready',height:0,count:0,states:{}})),priority={ready:0,repost:1,pending:2,loading:3,partial:4};
      this.items.forEach((item,i)=>{const x=Math.min(width-1,Math.floor((this.position(i)+this.position(i+1,true))/2*width)),bin=bins[x],status=item.repost&&item.state==='ready'?'repost':item.state;bin.count++;bin.states[status]=(bin.states[status]||0)+1;if(priority[status]>priority[bin.state])bin.state=status;bin.height=Math.max(bin.height,item.id?Math.min(43,5+Math.log2(1+(item.expected_comments||item.comments||0))*4):4);});
      // Keep the mix visible at high density; every nonempty state gets at least one pixel vertically.
      bins.forEach((bin,x)=>{let y=73,remaining=bin.height;const states=['partial','loading','pending','repost','ready'].filter(s=>bin.states[s]);states.forEach((state,i)=>{const h=i===states.length-1?remaining:Math.min(remaining-(states.length-i-1),Math.max(1,bin.height*bin.states[state]/bin.count));ctx.fillStyle=this.palette[state]||this.palette.pending;ctx.fillRect(x,y-h,1,h);y-=h;remaining-=h;});});this.overviewBins=bins;
    }
    if(this.overview.width!==this.cache.width||this.overview.height!==this.cache.height){this.overview.width=this.cache.width;this.overview.height=this.cache.height;}
    const ctx=this.overview.getContext('2d');ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,this.overview.width,this.overview.height);ctx.drawImage(this.cache,0,0);ctx.scale(dpr,dpr);if(!this.items.length)return;
    const left=this.position(start)*width,right=this.position(end,true)*width,x=Math.max(0,Math.min(width-6,(left+right)/2-Math.max(6,right-left)/2)),span=Math.min(width-x,Math.max(6,right-left));ctx.fillStyle=this.palette.window;ctx.globalAlpha=.1;ctx.fillRect(x,23,span,52);ctx.globalAlpha=1;ctx.strokeStyle=this.palette.window;ctx.lineWidth=1.5;ctx.strokeRect(x+.75,23.75,Math.max(1,span-1.5),51);ctx.fillRect(x+span/2-1,42,2,14);
    const activeIndex=this.items.findIndex(i=>i.id===this.active);if(activeIndex>=0){const ax=(this.position(activeIndex)+this.position(activeIndex+1,true))/2*width;ctx.fillStyle=this.palette.loading;ctx.fillRect(ax,26,1,49);ctx.beginPath();ctx.arc(ax,80,2,0,Math.PI*2);ctx.fill();}
  }
};
if(document.getElementById('archive-timeline')&&window.TIMELINE)new window.ArchiveTimeline(document.getElementById('archive-timeline')).set(window.TIMELINE);
if(document.getElementById('archive-timeline')?.dataset.source){const root=document.getElementById('archive-timeline'),view=new window.ArchiveTimeline(root);const refresh=async()=>{try{const r=await fetch(root.dataset.source,{cache:'no-store'});if(r.ok){const data=await r.json();view.set(data,data.active);}}catch(e){view.count.textContent='Нет соединения · показаны сохранённые данные';}finally{setTimeout(refresh,8000);}};refresh();}
