'use strict';
// The journal's whole history above (one stick per pixel column, years equal or by entry count);
// below, every entry in its own fixed slot, virtualized and scrolled sideways.
// The frame on the map is exactly the stretch of entries visible below: narrow where the journal was
// dense, wide where it was quiet. Dragging the frame, clicking the map or a year scrolls the entries.
window.ArchiveTimeline=class {
  constructor(root,base='',options={}){
    root.timelineView?.destroy();root.timelineView=this;
    this.root=root;this.base=base;this.items=[];this.active=null;this.slot=12;this.bands=[];this.scale='time';
    this.options={summary:true,collapsible:false,open:true,...options};this.open=!this.options.collapsible||this.options.open!==false;
    this.follow=true;this.placed=false;
    this.node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
    const node=this.node,button=(text,label,action,cls='btn text')=>{const b=node('button',cls,text);b.type='button';b.setAttribute('aria-label',label);b.onclick=action;return b;};
    root.setAttribute('aria-live','off');
    this.head=node('div','timeline-head');this.count=node('span','muted small');this.head.append(this.count);this.head.hidden=!this.options.summary;
    // Map: sticks on canvas, the frame is an element so it can be focused and dragged.
    this.map=node('div','timeline-map');this.overview=node('canvas','timeline-overview');this.overview.setAttribute('aria-hidden','true');this.cache=document.createElement('canvas');
    this.frame=node('div','timeline-window');this.frame.tabIndex=0;this.frame.setAttribute('role','slider');
    this.frame.setAttribute('aria-label','Видимые записи на шкале истории. Стрелки — листать, Home и End — начало и конец');
    this.map.append(this.overview,this.frame);
    this.years=node('div','timeline-years');
    this.hint=node('p','timeline-hint','Щёлкните по шкале, чтобы показать записи этого времени.');
    // Entries.
    this.detail=node('section','timeline-detail');
    this.detailHead=node('div','timeline-detail-head');const range=node('div','timeline-range');this.range=node('strong');this.rangeCount=node('span','muted small');range.append(this.range,this.rangeCount);
    const tools=node('div','timeline-nav');
    this.gap=button('Пропуски','Следующая запись с пропусками',()=>this.nextGap());
    this.current=button('К текущей','К загружаемой записи',()=>{this.follow=true;this.jump(this.items.findIndex(i=>i.id===this.active));});
    this.prev=button('←','Предыдущие записи',()=>{this.follow=false;this.shift(-1);},'icon');this.next=button('→','Следующие записи',()=>{this.follow=false;this.shift(1);},'icon');
    this.scaleControl=node('div','timeline-scale');this.scaleControl.setAttribute('role','group');this.scaleControl.setAttribute('aria-label','Масштаб общей шкалы');
    for(const [value,text] of [['time','времени'],['posts','записям']]){const b=node('button');b.type='button';b.dataset.scale=value;b.append(node('span','timeline-wide','по '),document.createTextNode(text));b.onclick=()=>{this.scale=value;this.yearsKey=null;this.invalidate();};this.scaleControl.append(b);}
    const help=node('details','timeline-help'),summary=node('summary','','?');summary.setAttribute('aria-label','Как читать хронологию');
    help.append(summary,node('p','','Столбик — одна запись. Высота — сколько в ней комментариев и изображений (шкала логарифмическая), заливка — сколько уже сохранено, штриховка — чего-то не хватает. Рамка на общей шкале — записи, видимые внизу: тяните её или щёлкните по шкале, чтобы перейти; щелчок по году — к его первой записи. На общей шкале несколько записей делят один пиксель; пропуски на ней всё равно видны.'));
    this.close=button('×','Свернуть записи',()=>{this.open=false;this.invalidate();this.years.querySelector('button:not(:disabled):not([hidden])')?.focus({preventScroll:true});},'icon timeline-close');this.close.hidden=!this.options.collapsible;
    tools.append(this.gap,this.current,this.prev,this.next,this.scaleControl,help,this.close);
    this.detailHead.append(range,tools);
    this.viewport=node('div','timeline-viewport');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Записи по порядку. Стрелки — листать, Home и End — начало и конец.');
    this.track=node('div','timeline-track');this.viewport.append(this.track);
    this.legend=node('ul','timeline-legend');this.legend.setAttribute('aria-label','Обозначения');
    this.detail.append(this.detailHead,this.viewport,this.legend);
    // A card beside the bar, outside the page flow: hovering never moves the controls below.
    this.tip=node('div','timeline-tip');this.tip.hidden=true;this.tip.id='timeline-tip-'+Math.random().toString(36).slice(2);
    root.classList.add('timeline-root');root.replaceChildren(this.head,this.map,this.years,this.hint,this.detail,this.tip);
    this.viewport.onscroll=()=>{this.hideTip();this.schedule();};
    for(const type of ['wheel','pointerdown','touchstart'])this.viewport.addEventListener(type,()=>{this.follow=false;},{passive:true});
    const barOf=target=>target?.closest?.('.timeline-bar');
    this.track.onpointerover=e=>{const bar=barOf(e.target);if(bar)this.showTip(bar);};
    this.track.onpointerleave=()=>{if(!this.track.contains(document.activeElement))this.hideTip();};
    this.track.addEventListener('focusin',e=>{const bar=barOf(e.target);if(bar)this.showTip(bar);});
    this.track.addEventListener('focusout',e=>{if(!this.track.contains(e.relatedTarget))this.hideTip();});
    root.addEventListener('keydown',e=>{if(e.key==='Escape'&&!this.tip.hidden){this.hideTip();}});
    this.viewport.onkeydown=e=>{if(e.target!==this.viewport)return;if(['ArrowRight','ArrowLeft','Home','End'].includes(e.key)){e.preventDefault();this.follow=false;this.jump(e.key==='Home'?0:e.key==='End'?this.items.length-1:Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)+(e.key==='ArrowRight'?10:-10)),e.key==='End');}};
    this.frame.onkeydown=e=>{const act={ArrowRight:()=>this.shift(1),ArrowLeft:()=>this.shift(-1),PageDown:()=>this.shift(1),PageUp:()=>this.shift(-1),Home:()=>this.jump(0,false),End:()=>this.jump(this.items.length-1)}[e.key];if(act){e.preventDefault();this.follow=false;act();}};
    const ratio=e=>{const box=this.map.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-box.left)/box.width));};
    const begin=e=>{
      const index=this.indexAt(ratio(e));this.hideTip();this.follow=false;
      if(!this.open){this.open=true;this.invalidate();this.jump(index);return;}
      const start=this.viewport.scrollLeft/this.slot,visible=this.viewport.clientWidth/this.slot;
      this.dragOffset=e.target===this.frame&&index>=start&&index<start+visible?index-start:visible/2;this.drag=true;
      if(e.pointerId!=null&&!this.map.hasPointerCapture(e.pointerId))this.map.setPointerCapture(e.pointerId);
      this.jump(Math.max(0,index-this.dragOffset),false);
    };
    this.map.onpointerdown=e=>{
      if(e.button!==0||!this.items.length)return;
      // A finger may be scrolling the page: act on a tap or a sideways drag, not on touch-down.
      if(e.pointerType==='touch'){this.touch={x:e.clientX,y:e.clientY,target:e.target,pointerId:e.pointerId};return;}
      begin(e);
    };
    this.map.onpointermove=e=>{
      if(this.touch&&!this.drag){const dx=e.clientX-this.touch.x,dy=e.clientY-this.touch.y;if(Math.abs(dx)>8&&Math.abs(dx)>Math.abs(dy)){begin({clientX:this.touch.x,target:this.touch.target,pointerId:this.touch.pointerId});this.touch=null;}return;}
      if(this.drag){this.jump(Math.max(0,this.indexAt(ratio(e))-this.dragOffset),false);return;}
      const band=this.bandAt(ratio(e));if(band)this.map.title=`${band.label} · ${window.ArchiveTimeline.count(band.count,['запись','записи','записей'])}${band.partial?' · с пропусками: '+band.partial:''}`;
    };
    const release=e=>{this.drag=false;if(this.map.hasPointerCapture(e.pointerId))this.map.releasePointerCapture(e.pointerId);};
    this.map.onpointerup=e=>{if(this.touch){const tap=this.touch;this.touch=null;if(Math.hypot(e.clientX-tap.x,e.clientY-tap.y)<8){begin({clientX:tap.x,target:tap.target,pointerId:null});this.drag=false;}}release(e);};
    this.map.onpointercancel=e=>{this.touch=null;release(e);};
    this.resize=new ResizeObserver(()=>{this.fit();this.invalidate();});this.resize.observe(this.root);
    this.theme=new MutationObserver(()=>this.invalidate());this.theme.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  }
  destroy(){this.resize?.disconnect();this.theme?.disconnect();cancelAnimationFrame(this.frameRequest);}
  // A short journal spreads its slots over the width instead of huddling in a corner.
  baseSlot(){const w=this.viewport.clientWidth;return w&&(w<560||matchMedia('(pointer: coarse)').matches)?44:12;}
  fit(){const width=this.viewport.clientWidth,n=this.items.length;if(!width)return;const base=this.baseSlot();const slot=n&&n*base<width?Math.min(48,Math.max(base,Math.floor(width/n))):base;if(slot!==this.slot){const old=this.slot;this.slot=slot;this.track.style.width=Math.max(1,n*slot)+'px';this.viewport.scrollLeft=this.viewport.scrollLeft/old*slot;}}
  showTip(bar){
    const item=this.items[Number(bar.dataset.timelineIndex)];if(!item)return;
    const card=this.card(item);this.tip.replaceChildren(...card);this.tip.hidden=false;this.tipIndex=bar.dataset.timelineIndex;
    bar.setAttribute('aria-describedby',this.tip.id);
    const root=this.root.getBoundingClientRect(),box=bar.getBoundingClientRect(),column=bar.querySelector('.timeline-column')?.getBoundingClientRect()||box;
    const width=this.tip.offsetWidth,height=this.tip.offsetHeight,center=box.left+box.width/2-root.left;
    const left=Math.max(0,Math.min(root.width-width,center-width/2)),top=Math.max(0,column.top-root.top-height-10);
    this.tip.style.left=left+'px';this.tip.style.top=top+'px';this.tip.style.setProperty('--arrow',Math.max(12,Math.min(width-12,center-left))+'px');
  }
  hideTip(){this.tip.hidden=true;this.tipIndex=null;}
  schedule(){if(this.frameRequest)return;this.frameRequest=requestAnimationFrame(()=>{this.frameRequest=null;this.draw();});}
  invalidate(){this.cacheKey=null;this.drawKey=null;this.draw();}
  shift(direction){this.jump(Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)+direction*Math.max(1,Math.floor(this.viewport.clientWidth/this.slot*.8))),false);}
  jump(index,center=true){if(index>=0&&this.items.length){if(!this.open){this.open=true;this.invalidate();}this.viewport.scrollLeft=Math.max(0,index*this.slot-(center?this.viewport.clientWidth/2-this.slot/2:0));this.draw();}}
  nextGap(){
    this.follow=false;const after=(this.lastGap??Math.floor(this.viewport.scrollLeft/this.slot))+1;
    let next=this.items.findIndex((i,n)=>n>=after&&i.state==='partial');if(next<0)next=this.items.findIndex(i=>i.state==='partial');if(next<0)return;
    this.lastGap=next;this.jump(next);this.track.querySelector(`[data-timeline-index="${next}"]`)?.focus({preventScroll:true});
  }
  toYear(label){const band=this.bands.find(b=>b.label===String(label));if(band?.count){this.follow=false;this.jump(band.start,false);}}
  set(data,active=null){
    const anchor=this.placed&&this.items[Math.floor(this.viewport.scrollLeft/this.slot)],offset=this.viewport.scrollLeft%this.slot;
    this.data=data;this.active=active;const rows=[...data.entries];for(const gap of data.gaps||[])rows.push({date:gap.date,count:gap.count,state:'pending'});
    rows.sort((a,b)=>(a.date||'').localeCompare(b.date||'')||(a.id||0)-(b.id||0));this.items=[];for(const row of rows){if(row.id)this.items.push(row);else for(let n=0;n<row.count;n++)this.items.push({...row,count:undefined});}
    const known=this.items.filter(i=>/^\d{4}-/.test(i.date||'')),unknown=this.items.filter(i=>!/^\d{4}-/.test(i.date||''));this.items=[...known,...unknown];
    const groups=new Map();this.items.forEach((item,index)=>{const label=/^\d{4}-/.test(item.date||'')?item.date.slice(0,4):'Без даты';if(!groups.has(label))groups.set(label,{label,start:index,count:0,partial:0});const g=groups.get(label);g.count++;if(item.state==='partial')g.partial++;});
    this.bands=[];if(known.length){for(let year=Number(known[0].date.slice(0,4));year<=Number(known.at(-1).date.slice(0,4));year++){const label=String(year);this.bands.push(groups.get(label)||{label,start:0,count:0,partial:0});}}if(unknown.length)this.bands.push(groups.get('Без даты'));
    this.bandForItem=new Int32Array(this.items.length);this.bands.forEach((b,index)=>{for(let i=b.start;i<b.start+b.count;i++)this.bandForItem[i]=index;});
    const T=window.ArchiveTimeline;this.count.textContent=`Сохранено ${Number(data.ready||0).toLocaleString('ru-RU')} из ${T.count(data.expected,['записи','записей','записей'])}${data.partial?` · ${T.count(data.partial,['запись','записи','записей'])} с пропусками`:''}`;
    const present=new Set(this.items.map(i=>i.repost&&i.state==='ready'?'repost':i.state));
    this.legend.replaceChildren(...[['ready','сохранена'],['loading','загружается'],['partial','есть пропуски'],['repost','перепост'],['pending','ещё не получена']].filter(([cls])=>present.has(cls)).map(([cls,text])=>this.node('li',cls,text)));
    this.legend.hidden=[...present].every(s=>s==='ready');
    this.gap.hidden=!data.partial;this.yearsKey=null;this.fit();this.track.style.width=Math.max(1,this.items.length*this.slot)+'px';
    const activeIndex=active?this.items.findIndex(i=>i.id===active):-1;
    // Follow the entry being loaded until the person moves the entries themselves.
    if(activeIndex>=0&&this.follow&&this.viewport.clientWidth){this.placed=true;this.viewport.scrollLeft=Math.max(0,activeIndex*this.slot-this.viewport.clientWidth/2+this.slot/2);}
    else if(anchor?.id){const index=this.items.findIndex(i=>i.id===anchor.id);if(index>=0)this.viewport.scrollLeft=index*this.slot+offset;}
    else if(!this.placed&&this.viewport.clientWidth){this.placed=true;this.viewport.scrollLeft=this.items.length*this.slot;}
    this.invalidate();
  }
  bandBox(band,n){const total=Math.max(1,this.items.length);return this.scale==='posts'?[band.start/total,band.count/total]:[n/this.bands.length,1/this.bands.length];}
  bandAt(ratio){return this.scale==='posts'?this.bands[this.bandForItem[Math.max(0,Math.min(this.items.length-1,Math.floor(ratio*this.items.length)))]]:this.bands[Math.min(this.bands.length-1,Math.max(0,Math.floor(ratio*this.bands.length)))];}
  fraction(index,end=false){
    const item=this.items[index],band=this.bands[this.bandForItem[index]];
    if(this.scale==='time'&&band.label!=='Без даты'){
      const m=/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(item.date||'');
      if(m){const year=Number(m[1]),begin=Date.UTC(year,0,1),span=Date.UTC(year+1,0,1)-begin;
        return Math.max(0,Math.min(1,(Date.UTC(year,Number(m[2])-1,Number(m[3]),Number(m[4]||12),Number(m[5]||0))+(end?1000:0)-begin)/span));}
    }
    return (index-band.start+(end?1:0))/band.count;
  }
  indexAt(ratio){if(!this.bands.length)return 0;if(this.scale==='posts')return Math.max(0,Math.min(this.items.length-1,Math.floor(ratio*this.items.length)));const pos=Math.max(0,Math.min(.999999,ratio))*this.bands.length,b=Math.floor(pos),band=this.bands[b];if(band.count){let lo=band.start,hi=lo+band.count;while(lo<hi){const mid=(lo+hi)>>1;if(this.fraction(mid)<pos-b)lo=mid+1;else hi=mid;}return Math.min(band.start+band.count-1,lo);}return this.bands.slice(b+1).find(y=>y.count)?.start??Math.max(0,this.items.length-1);}
  position(index,end=false){if(!this.items.length)return 0;if(this.scale==='posts')return Math.max(0,Math.min(this.items.length,index))/this.items.length;const i=Math.max(0,Math.min(this.items.length-1,Math.floor(index-(end?1:0))));return (this.bandForItem[i]+this.fraction(i,end))/this.bands.length;}
  static plural(n,forms){const a=Math.abs(n)%100,b=a%10;return forms[a>10&&a<20?2:b===1?0:b>1&&b<5?1:2];}
  static count(n,forms){return `${Number(n||0).toLocaleString('ru-RU')} ${window.ArchiveTimeline.plural(Number(n||0),forms)}`;}
  static date(value){const m=/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(value||'');if(!m)return 'дата неизвестна';const month='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(' ')[Number(m[2])-1];return `${Number(m[3])} ${month} ${m[1]}${m[4]?`, ${m[4]}:${m[5]}`:''}`;}
  facts(item){
    const T=window.ArchiveTimeline,COMMENTS=['комментарий','комментария','комментариев'],OF_COMMENTS=['комментария','комментариев','комментариев'],IMAGES=['изображение','изображения','изображений'],OF_IMAGES=['изображения','изображений','изображений'];
    if(!item.id)return {title:'Запись ещё не получена',date:T.date(item.date),lines:[],warn:''};
    const m=item.materials||{},lines=[],warn=[];
    if(item.repost)lines.push('Перепост');
    else{
      const expected=item.expected_comments,got=item.comments||0;
      lines.push(item.comments_skipped&&!got?'Комментарии не запрашивались':item.comments_complete||expected==null||got>=expected?(got?T.count(got,COMMENTS):'Без комментариев'):`${got.toLocaleString('ru-RU')} из ${T.count(expected,OF_COMMENTS)}`);
      if(item.screened)lines[lines.length-1]+=` · ${T.count(item.screened,['скрытый','скрытых','скрытых'])}`;
    }
    if(m.requested)lines.push(m.saved>=m.requested?T.count(m.requested,IMAGES):`${(m.saved||0).toLocaleString('ru-RU')} из ${T.count(m.requested,OF_IMAGES)}`);
    if(item.error&&!item.repost)warn.push(item.settled?'ЖЖ отдал не все комментарии':'Не все комментарии получены — запросим ещё раз');
    const lost=m.unavailable||0,waiting=Math.max(0,(m.missing||0)-lost);
    if(lost)warn.push(`Недоступно в ЖЖ: ${T.count(lost,IMAGES)}`);
    if(waiting&&item.comments_complete)warn.push(`Ещё не скачано: ${T.count(waiting,IMAGES)}`);
    if(item.videos)warn.push(`Не скачано видео: ${item.videos}`);
    return {title:item.title,date:T.date(item.date),lines,warn:warn.join('. ')};
  }
  description(item){const f=this.facts(item);return [f.title,f.date,...f.lines,f.warn].filter(Boolean).join('. ');}
  card(item){
    const f=this.facts(item),head=this.node('div','timeline-tip-head');head.append(this.node('strong','',f.title),this.node('span','timeline-tip-date',f.date));
    const nodes=[head];if(f.lines.length)nodes.push(this.node('div','timeline-tip-facts',f.lines.join(' · ')));if(f.warn)nodes.push(this.node('div','timeline-tip-warn',f.warn));
    if(item.id&&item.state==='loading')nodes.push(this.node('div','timeline-tip-facts','Загружается сейчас'));
    return nodes;
  }
  draw(){
    const open=this.open||!this.options.collapsible;
    this.detail.hidden=!open;this.frame.hidden=!open||!this.items.length;this.hint.hidden=open||!this.options.collapsible||!this.items.length||this.options.hint===false;
    this.scaleControl.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scale===this.scale)));
    const mapWidth=this.map.clientWidth;
    if(mapWidth)this.drawYears(mapWidth);
    const width=this.viewport.clientWidth;
    if(!open||!width){this.drawOverview(-1,-1);return;}
    if(!this.placed&&this.items.length){this.placed=true;this.fit();const ai=this.active?this.items.findIndex(i=>i.id===this.active):-1;this.viewport.scrollLeft=ai>=0?Math.max(0,ai*this.slot-width/2+this.slot/2):this.items.length*this.slot;}
    const visibleStart=Math.max(0,Math.floor(this.viewport.scrollLeft/this.slot)),visibleEnd=Math.min(this.items.length,Math.ceil((this.viewport.scrollLeft+width)/this.slot)),start=Math.max(0,visibleStart-2),end=Math.min(this.items.length,visibleEnd+2),key=[start,end,this.active,this.slot,width].join(':');
    if(this.drawKey!==key){
      this.drawKey=key;const focused=this.track.contains(document.activeElement)?document.activeElement.dataset.timelineIndex:undefined,nodes=[];
      for(const band of this.bands){if(!band.count||band.start>=end||band.start+band.count<=start)continue;const from=Math.max(start,band.start),to=Math.min(end,band.start+band.count),shade=this.node('span','timeline-year-band');shade.style.left=from*this.slot+'px';shade.style.width=(to-from)*this.slot+'px';if(Number(band.label)%2===0)shade.classList.add('even');nodes.push(shade);const x=Math.max(this.viewport.scrollLeft+4,band.start*this.slot+4),space=Math.min(this.viewport.scrollLeft+width,(band.start+band.count)*this.slot)-x;if(space>37){const label=this.node('span','timeline-year-mark',band.label);label.style.left=x+'px';nodes.push(label);}}
      let lastMonth='',lastTick=-100;
      for(let n=start;n<end;n++){
        const item=this.items[n],m=item.materials||{},total=Math.max(item.expected_comments||0,item.comments||0)+(m.requested||0)+1,done=1+(item.comments||0)+(m.saved||0);
        const bar=this.node(item.id?'a':'span','timeline-bar '+item.state+(item.repost?' repost':'')+(item.id===this.active?' active':''));bar.style.left=n*this.slot+'px';bar.style.width=this.slot+'px';bar.dataset.timelineIndex=String(n);if(item.id)bar.href=this.base+item.url;
        bar.setAttribute('aria-label',this.description(item));
        const track=this.node('span','timeline-column');track.style.height=(item.id?Math.min(116,18+Math.log2(total)*10):14)+'px';const fill=document.createElement('i');fill.style.height=(item.state==='ready'?100:Math.min(94,100*done/(total+1)))+'%';track.append(fill);bar.append(track);
        bar.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();this.follow=false;let target=Math.max(0,Math.min(this.items.length-1,e.key==='Home'?0:e.key==='End'?this.items.length-1:n+(e.key==='ArrowRight'?1:-1)));const direction=e.key==='ArrowLeft'||e.key==='End'?-1:1;while(this.items[target]&&!this.items[target].id)target+=direction;if(!this.items[target])return;const first=Math.floor(this.viewport.scrollLeft/this.slot),last=Math.floor((this.viewport.scrollLeft+this.viewport.clientWidth)/this.slot)-1;if(target<first||target>last)this.jump(target);this.track.querySelector(`[data-timeline-index="${target}"]`)?.focus({preventScroll:true});}};nodes.push(bar);
        const month=(item.date||'').slice(0,7),x=n*this.slot;if(month!==lastMonth&&x>=this.viewport.scrollLeft&&x-lastTick>65&&x<this.viewport.scrollLeft+width-25){const text=['янв.','февр.','март','апр.','май','июнь','июль','авг.','сент.','окт.','нояб.','дек.'][Number(month.slice(5))-1];if(text){const tick=this.node('span','timeline-month',text);tick.style.left=x+3+'px';nodes.push(tick);lastTick=x;}}lastMonth=month;
      }
      this.track.replaceChildren(...nodes);if(focused!==undefined)this.track.querySelector(`[data-timeline-index="${focused}"]`)?.focus({preventScroll:true});
      // Bars are rebuilt while loading: keep the open card on the same entry, or close it.
      if(this.tipIndex!=null){const bar=this.track.querySelector(`[data-timeline-index="${this.tipIndex}"]`);if(bar)this.showTip(bar);else this.hideTip();}
    }
    const first=this.items[visibleStart],last=this.items[visibleEnd-1],MON=['янв.','февр.','март','апр.','май','июнь','июль','авг.','сент.','окт.','нояб.','дек.'];
    if(first&&last){
      const a=/^(\d{4})-(\d{2})/.exec(first.date||''),z=/^(\d{4})-(\d{2})/.exec(last.date||'');
      const label=!a||!z?'Без даты':a[1]===z[1]?(a[2]===z[2]?`${MON[a[2]-1]} ${a[1]}`:`${MON[a[2]-1]} — ${MON[z[2]-1]} ${a[1]}`):`${MON[a[2]-1]} ${a[1]} — ${MON[z[2]-1]} ${z[1]}`;
      this.range.textContent=label;this.rangeCount.textContent=`${window.ArchiveTimeline.count(visibleEnd-visibleStart,['запись','записи','записей'])} из ${this.items.length.toLocaleString('ru-RU')}`;
      this.frame.setAttribute('aria-valuemin','1');this.frame.setAttribute('aria-valuemax',String(this.items.length));this.frame.setAttribute('aria-valuenow',String(visibleStart+1));this.frame.setAttribute('aria-valuetext',`${label}, записи ${visibleStart+1}–${visibleEnd} из ${this.items.length}`);
    }else{this.range.textContent='Записи ещё не получены';this.rangeCount.textContent='';}
    this.prev.disabled=visibleStart===0;this.next.disabled=visibleEnd>=this.items.length;
    const activeIndex=this.active?this.items.findIndex(i=>i.id===this.active):-1;this.current.hidden=activeIndex<0||(activeIndex>=visibleStart&&activeIndex<visibleEnd);
    if(mapWidth&&this.items.length){
      const left=this.position(visibleStart)*mapWidth,right=this.position(visibleEnd,true)*mapWidth,span=Math.min(mapWidth,Math.max(8,right-left)),x=Math.max(0,Math.min(mapWidth-span,(left+right)/2-span/2));
      this.frame.style.left=x+'px';this.frame.style.width=span+'px';
      for(const button of this.years.children){const band=this.bands[Number(button.dataset.band)];const [bx,bw]=this.bandBox(band,Number(button.dataset.band));button.classList.toggle('in',(bx+bw)*mapWidth>left&&bx*mapWidth<right);}
    }
    this.drawOverview(visibleStart,visibleEnd);
  }
  // Year labels under the map: first, last and as many between as fit; a click shows the year's first entry.
  drawYears(width){
    const key=[width,this.scale,this.bands.length,this.items.length].join(':');if(this.yearsKey===key)return;this.yearsKey=key;
    const focusedBand=this.years.contains(document.activeElement)?document.activeElement.dataset.band:null;
    const narrow=width<520,many=this.bands.length>12,buttons=[];
    this.bands.forEach((band,n)=>{
      const [x,w]=this.bandBox(band,n);if(this.scale==='posts'&&!band.count)return;
      const year=Number(band.label),edge=n===0||n===this.bands.length-1;
      const text=many&&!narrow&&!edge&&year%5&&/^\d{4}$/.test(band.label)?'’'+band.label.slice(2):band.label;
      const b=this.node('button','timeline-year',text);b.type='button';b.dataset.band=String(n);b.disabled=!band.count;
      b.setAttribute('aria-label',band.count?`${band.label}: ${window.ArchiveTimeline.count(band.count,['запись','записи','записей'])}, показать`:`${band.label}: записей нет`);
      b.style.left=(x+w/2)*width+'px';b.onclick=()=>{this.toYear(band.label);this.frame.focus({preventScroll:true});};
      b.dataset.priority=edge?'2':year%5===0?'1':'0';buttons.push(b);
    });
    this.years.replaceChildren(...buttons);
    // Edge labels stay inside the map instead of hanging over the card edge.
    for(const b of buttons){const half=b.offsetWidth/2,x=parseFloat(b.style.left);b.style.left=Math.max(half,Math.min(width-half,x))+'px';}
    if(focusedBand!=null)buttons.find(b=>b.dataset.band===focusedBand)?.focus({preventScroll:true});
    // Never two labels on top of each other: keep the ends and round years first.
    const placed=[];for(const level of ['2','1','0'])for(const b of buttons){if(b.dataset.priority!==level)continue;const box=b.getBoundingClientRect();if(placed.some(p=>box.left<p.right+4&&box.right>p.left-4))b.classList.add('crowded');else placed.push(box);}
  }
  drawOverview(start,end){
    const width=Math.round(this.map.clientWidth),height=Math.round(this.map.clientHeight)||60,dpr=window.devicePixelRatio||1,key=[width,height,dpr,this.scale,this.items.length].join(':');if(!width)return;
    const base=height-1,room=height-8;
    if(this.cacheKey!==key){
      this.cacheKey=key;this.cache.width=Math.round(width*dpr);this.cache.height=Math.round(height*dpr);const ctx=this.cache.getContext('2d');ctx.scale(dpr,dpr);
      const style=getComputedStyle(this.root),color=name=>style.getPropertyValue('--timeline-'+name).trim();this.palette={ready:color('ready'),pending:color('pending'),loading:color('loading'),partial:color('partial'),repost:color('repost'),window:color('window')};
      this.bands.forEach((band,n)=>{const [x,w]=this.bandBox(band,n);if(!w)return;if(n%2===0){ctx.fillStyle=color('band');ctx.fillRect(x*width,0,w*width,height);}});
      ctx.fillStyle=style.getPropertyValue('--line');ctx.fillRect(0,base,width,1);
      // One bin per CSS pixel; a gap wins over completed records sharing that pixel.
      const bins=Array.from({length:width},()=>({state:'ready',height:0,count:0,states:{}})),priority={ready:0,repost:1,pending:2,loading:3,partial:4};
      this.items.forEach((item,i)=>{const x=Math.min(width-1,Math.floor((this.position(i)+this.position(i+1,true))/2*width)),bin=bins[x],status=item.repost&&item.state==='ready'?'repost':item.state;bin.count++;bin.states[status]=(bin.states[status]||0)+1;if(priority[status]>priority[bin.state])bin.state=status;bin.height=Math.max(bin.height,item.id?Math.min(room,4+Math.log2(1+(item.expected_comments||item.comments||0))*room/12):4);});
      // Keep the mix visible at high density; every nonempty state gets at least one pixel vertically.
      const peak=Math.max(1,...bins.map(b=>b.count));
      bins.forEach((bin,x)=>{if(!bin.count)return;bin.height=Math.max(bin.height,6+(room-6)*.6*Math.log1p(bin.count)/Math.log1p(peak));let y=base,remaining=bin.height;ctx.globalAlpha=.45+.55*Math.sqrt(bin.count/peak);const states=['partial','loading','pending','repost','ready'].filter(s=>bin.states[s]);states.forEach((state,i)=>{const h=i===states.length-1?remaining:Math.min(remaining-(states.length-i-1),Math.max(1,bin.height*bin.states[state]/bin.count));ctx.fillStyle=this.palette[state]||this.palette.pending;ctx.fillRect(x,y-h,1,h);y-=h;remaining-=h;});
        // A gap stays visible even when it shares a pixel with many saved entries.
        if(bin.states.partial){ctx.globalAlpha=1;ctx.fillStyle=this.palette.partial;ctx.beginPath();ctx.arc(x+.5,base-bin.height-3.5,2.2,0,Math.PI*2);ctx.fill();}});ctx.globalAlpha=1;this.overviewBins=bins;
    }
    if(this.overview.width!==this.cache.width||this.overview.height!==this.cache.height){this.overview.width=this.cache.width;this.overview.height=this.cache.height;}
    const ctx=this.overview.getContext('2d');ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,this.overview.width,this.overview.height);ctx.drawImage(this.cache,0,0);ctx.scale(dpr,dpr);if(!this.items.length)return;
    const activeIndex=this.items.findIndex(i=>i.id===this.active);if(activeIndex>=0){const ax=(this.position(activeIndex)+this.position(activeIndex+1,true))/2*width;ctx.fillStyle=this.palette.loading;ctx.fillRect(ax,4,1,base-4);ctx.beginPath();ctx.arc(ax,4,2.5,0,Math.PI*2);ctx.fill();}
  }
};
if(document.getElementById('archive-timeline')&&window.TIMELINE)new window.ArchiveTimeline(document.getElementById('archive-timeline')).set(window.TIMELINE);
if(document.getElementById('archive-timeline')?.dataset.source){const root=document.getElementById('archive-timeline'),view=new window.ArchiveTimeline(root);const refresh=async()=>{try{const r=await fetch(root.dataset.source,{cache:'no-store'});if(r.ok){const data=await r.json();view.set(data,data.active);}}catch(e){view.count.textContent='Нет соединения · показаны сохранённые данные';}finally{setTimeout(refresh,8000);}};refresh();}
