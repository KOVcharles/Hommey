(async function () {
    const response = await fetch('journey-flow-assets/data.json');
    if (!response.ok) { document.getElementById('demoStatus').textContent = '演示快照加载失败，请刷新重试。'; return; }
    const data = await response.json();
    const standards = {kind:'policy',title:'差旅标准',status:'partial',goal_id:'demo-policy',body:'以下金额为界面排版示例。尚未提供职级，正式查询会保留各档位及适用条件。',items:[
        {label:'住宿限额 · 各职级',value:'普通员工 300元/晚\n部门经理 360元/晚\n总监及以上 420元/晚',detail:'排版示例 · 实际按目的地及企业制度核实'},
        {label:'餐费补贴',value:'60元/天\n按自然日计发，在途不足半天按半日计。',detail:'排版示例 · 无需餐费票据'},
        {label:'高铁 / 动车',value:'普通员工二等座；部门经理及以上一等座。副总经理及以上，或单程超过6小时，可乘商务座。'},
        {label:'航空出行',value:'普通员工经济舱。部门经理及以上且单程超过4小时，或总监及以上，可乘商务舱。'},
        {label:'市内交通',value:'出租车、网约车据实报销，单程超过200元需备注事由；地铁、公交据实报销。共享单车、电动车每日累计上限30元。'},
        {label:'住宿核算与凭证',value:'按实际入住间夜核算，保留住宿发票；含税与服务费，不含早餐。超出限额部分按公司制度处理。'}]};
    function enrich(choice) {
        const value = structuredClone(choice), board = value.trip_options;
        value.sections.push(structuredClone(standards),{kind:'trip',title:'每日安排',status:'partial',goal_id:'demo-itinerary',body:'',items:[
            {label:board.start_date,value:'3 项安排',activities:['去程：比较车次快照，具体班次待选择。',`工作：${board.anchor.name}，会议开始时间待补充。`,'住宿：优先查看地图上符合品牌偏好的附近酒店。']},
            {label:board.end_date,value:'2 项安排',activities:['工作：按会议实际结束时间安排后续事项。','返程：结束时间确认后查询返程车次，预留到站与检票时间。']}]});
        value.notices=['补充职级，确认住宿限额与可报销席别。','补充会议开始和结束时间，以便核对去程及查询返程。'];
        board.next_actions=[];
        value.sources=[{title:'差旅制度示例 · 用于界面预览',detail:'正式卡片会展示实际检索的文件与条款位置。'}];
        return value;
    }
    window.HommeyTripChoices.configure({searchPlaces:async(city,keyword)=>city.replace(/市$/,'')==='重庆'?data.places.filter(p=>[p.name,p.address,p.district].join(' ').includes(keyword)):[]});
    window.HommeyJourneyMap.configure({loadMap:async(city,id,zoom,signal)=>{const url=data.maps[id]?.[zoom];if(!url)throw new Error('没有地图快照');return (await fetch(url,{signal})).blob();}});
    let current = enrich(data.choices[0]);
    const cards=document.getElementById('cards');
    function show(policy) {
        document.getElementById('journeyTab').setAttribute('aria-pressed', String(!policy));
        document.getElementById('policyTab').setAttribute('aria-pressed', String(policy));
        cards.replaceChildren(policy ? HommeyPolicyCard.create({title:'差旅标准 · 排版示例',sections:[standards],sources:current.sources}) : HommeyTripChoices.create(current));
    }
    document.getElementById('journeyTab').onclick=()=>show(false);
    document.getElementById('policyTab').onclick=()=>show(true);
    document.addEventListener('hommey:submit-message', event=>{
        const id=event.detail.requestPayload?.trip_input?.work_location_place_id;
        const found=data.choices.find(c=>c.trip_options.anchor.provider_place_id===id);
        if(!found){event.preventDefault();document.getElementById('demoStatus').textContent='预览提供重庆国际会议展览中心和悦来国际会议中心两个地点。';return;}
        setTimeout(()=>{current=enrich(found);const card=HommeyTripChoices.create(current);cards.append(card);event.detail.complete?.(true);card.scrollIntoView({behavior:'smooth',block:'start'});document.getElementById('demoStatus').textContent='已生成新会场对应的卡片，酒店和每日安排同步更新；旧卡保留。';},180);
    });
    show(false);
})();
