"""Read-only live routes/provider check plus public snapshots for UI acceptance."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import date, timedelta
import httpx
from fastapi import FastAPI
from core.integrations.travel_info import TravelInformationService
from core.integrations.trains import create_train_query_backend
from agent_runtime.trip_options import collect_options, options_output
from core.presentation.trip_intake_document import build_trip_intake_document
from webui_new.routes.places import create_places_router
from webui_new.auth import require_path_user

async def main():
    travel=TravelInformationService()
    app=FastAPI(); app.dependency_overrides[require_path_user]=lambda: SimpleNamespace(id='read-only-probe')
    app.include_router(create_places_router(travel.places))
    root=Path('webui_new/static/design-demos/journey-flow-assets');root.mkdir(exist_ok=True)
    data={'choices':[], 'maps':{}, 'places':[]}
    service=SimpleNamespace(travel=travel,trains=create_train_query_backend(),memory=SimpleNamespace(long_term=SimpleNamespace(get_preference=lambda:{'hotel_brands':['汉庭']})))
    start=date.today()+timedelta(days=1)
    trip={'origin':'北京','destination':'重庆','start_date':str(start),'end_date':str(start+timedelta(days=1)),'duration_days':2,'trip_purpose':'参加会议'}
    data['intake']=build_trip_intake_document(trip).model_dump(mode='json')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        for index, keyword in enumerate(['重庆国际会议展览中心','重庆悦来国际会议中心']):
            response=await client.get('/api/read-only-probe/places/suggest',params={'city':'重庆','keyword':keyword})
            response.raise_for_status(); result=response.json()['items'][0]
            assert result['city']=='重庆市' and result['location']
            data['places'].append(result)
            anchor=await travel.places.verify(result['place_id'])
            board=await collect_options(service,{**trip,'work_location':anchor.name,'work_location_verified':anchor.model_dump(mode='json')})
            data['choices'].append(options_output(board)['answer_document'])
            maps={}
            for zoom in [12,13,14,15]:
                response=await client.get('/api/read-only-probe/places/map',params={'city':'重庆','place_id':result['place_id'],'zoom':zoom})
                response.raise_for_status(); assert response.content.startswith(b'\x89PNG')
                file=f'{index}-{zoom}.png'; (root/file).write_bytes(response.content);maps[str(zoom)]=f'journey-flow-assets/{file}'
            data['maps'][result['place_id']]=maps
            print('live_choice',anchor.name,'trains',len(board.trains),'hotels',len(board.hotels),flush=True)
    (root/'data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print('live_routes_maps_and_choices_ok')

if __name__=='__main__': asyncio.run(main())
