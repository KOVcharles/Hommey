(async function(){
    const data=await (await fetch('journey-flow-assets/data.json')).json();
    window.HommeyTripChoices.configure({searchPlaces:async(city,keyword)=>city.replace(/市$/,'')==='重庆'?data.places.filter(p=>[p.name,p.address,p.district].join(' ').includes(keyword)):[]});
    window.HommeyJourneyMap.configure({loadMap:async(city,id,zoom,signal)=>{const url=data.maps[id]?.[zoom];if(!url)throw new Error('没有地图快照');return (await fetch(url,{signal})).blob();}});
    function start(){document.getElementById('cards').replaceChildren(HommeyTripIntakeCard.create(data.intake));}
    document.getElementById('resetDemo').onclick=start;
    document.addEventListener('hommey:submit-message',event=>{
        const id=event.detail.requestPayload?.trip_input?.work_location_place_id;
        const choice=data.choices.find(c=>c.trip_options.anchor.provider_place_id===id);
        if(!choice){event.preventDefault();document.getElementById('demoStatus').textContent='预览仅提供国际会展中心、悦来国际会议中心两个真实样本。';return;}
        setTimeout(()=>{const card=HommeyTripChoices.create(choice);document.getElementById('cards').append(card);event.detail.complete?.(true);card.scrollIntoView({behavior:'smooth',block:'start'});document.getElementById('demoStatus').textContent='已展示该会场对应的酒店快照；正式聊天会重新调用接口查询。';},150);
    });start();
})();
