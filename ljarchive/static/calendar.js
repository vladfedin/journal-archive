'use strict';
document.addEventListener('DOMContentLoaded', () => {
  if (!window.CALENDAR) return;
  const data=window.CALENDAR, grid=document.getElementById('calendar-grid'), select=document.getElementById('calendar-year');
  const months='Январь Февраль Март Апрель Май Июнь Июль Август Сентябрь Октябрь Ноябрь Декабрь'.split(' ');
  const genitive='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(' ');
  const weekdays=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
  const years=Object.keys(data.years).sort(), days=data.days;
  const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
  const count=n=>`${n} ${n%10===1&&n%100!==11?'запись':n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?'записи':'записей'}`;
  for (const year of years) {const o=el('option','',year);o.value=year;select.append(o);}
  const requested=new URLSearchParams(location.search).get('year');
  select.value=years.includes(requested)?requested:years.at(-1)||'';
  document.getElementById('calendar-undated').textContent=data.undated?` · Без даты: ${data.undated}`:'';
  function render() {
    const year=select.value, position=years.indexOf(year);
    grid.replaceChildren();
    select.disabled=!years.length;
    document.getElementById('calendar-prev').disabled=position<=0;
    document.getElementById('calendar-next').disabled=position<0||position===years.length-1;
    document.getElementById('calendar-count').textContent=count(data.years[year]||0);
    if(!year){grid.append(el('p','muted','Записей с датами пока нет'));return;}
    for(let month=0;month<12;month++) {
      const prefix=year+'-'+String(month+1).padStart(2,'0'), table=el('table','calendar-month');
      const first=new Date(0);first.setUTCFullYear(Number(year),month,1);first.setUTCHours(0,0,0,0);
      const last=new Date(first);last.setUTCMonth(month+1,0);
      const size=last.getUTCDate(), offset=(first.getUTCDay()+6)%7;
      let total=0;for(let d=1;d<=size;d++)total+=days[prefix+'-'+String(d).padStart(2,'0')]||0;
      const caption=el('caption'), title=el(total?'a':'span','calendar-month-title',months[month]);
      if(total){title.href='index.html?month='+prefix+'&view=full';title.setAttribute('aria-label',`${months[month]} ${year}: ${count(total)}`);}
      caption.append(title,el('span','calendar-month-count',count(total)));table.append(caption);
      const head=el('thead'), header=el('tr');
      for(const label of weekdays){const th=el('th','',label);th.scope='col';header.append(th);}head.append(header);table.append(head);
      const body=el('tbody');
      for(let week=0;week<Math.ceil((offset+size)/7);week++) {
        const row=el('tr');
        for(let weekday=0;weekday<7;weekday++) {
          const cell=el('td'), d=week*7+weekday-offset+1;
          if(d>0&&d<=size) {
            const date=prefix+'-'+String(d).padStart(2,'0'), n=days[date]||0;
            const item=el(n?'a':'span','calendar-day'+(n?' heat-'+Math.min(5,1+Math.floor(Math.log2(n))):''));
            item.append(el('span','calendar-date',d));
            if(n){item.href='index.html?date='+date+'&view=full';item.append(el('strong','calendar-day-count',n));}
            const label=`${d} ${genitive[month]} ${year}: ${count(n)}`;item.title=label;item.setAttribute('aria-label',label);
            cell.append(item);
          }
          row.append(cell);
        }
        body.append(row);
      }
      table.append(body);grid.append(table);
    }
    try{history.replaceState(null,'',location.pathname+'?year='+year);}catch{}
  }
  select.addEventListener('change',render);
  document.getElementById('calendar-prev').onclick=()=>{select.value=years[years.indexOf(select.value)-1];render();};
  document.getElementById('calendar-next').onclick=()=>{select.value=years[years.indexOf(select.value)+1];render();};
  render();
});
