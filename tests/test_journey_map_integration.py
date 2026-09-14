from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI

from agent_runtime.contracts import Scope
from agent_runtime.engine import Supervisor
from core.integrations.places.amap import AMapProvider, AMapError
from core.integrations.places.models import HotelCandidate
from core.presentation.trip_intake_document import build_trip_intake_document
from tests.test_trip_choices import services, trip, place
from tests.test_supervisor_runtime import FakeStore, CONFIG, SCOPE
from webui_new.auth import require_path_user
from webui_new.routes.places import create_places_router
from webui_new.routes.chat import _prepare_chat_input
from webui_new.schemas.requests import ChatRequest
from webui_new.core.errors import BusinessError


def test_first_intake_carries_fields_for_verified_place_submission():
    document = build_trip_intake_document(trip(False))
    assert document.place_selection_required and document.status == "collecting_required"
    assert document.title == "确认会议或办公地点"
    assert document.trip_input["origin"] == "北京"
    assert not document.trip_input.get("work_location_place_id")
    raw=trip(False);raw['work_location']='重庆国际会议展览中心'
    unverified=build_trip_intake_document(raw)
    assert all(field.key!='work_location' for field in unverified.collected)
    assert unverified.trip_input['work_location']==raw['work_location']


@pytest.mark.asyncio
async def test_map_route_checks_city_and_does_not_expose_credentials():
    calls = []
    class Places:
        async def verify(self, poi):
            return place() if poi == 'P1' else place('南京市')
        async def static_map(self, p, zoom):
            calls.append((p.provider_place_id, zoom))
            return b'\x89PNG\r\n\x1a\nfixture'
    app = FastAPI()
    app.dependency_overrides[require_path_user] = lambda: SimpleNamespace(id='user')
    app.include_router(create_places_router(Places()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/user/places/map', params={'city':'重庆','place_id':'P1','zoom':13})
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['cache-control'].startswith('private')
        assert response.content.startswith(b'\x89PNG') and calls == [('P1',13)]
        with pytest.raises(BusinessError, match='所选地点不属于目的地'):
            await client.get('/api/user/places/map', params={'city':'重庆','place_id':'N1'})
        assert len(calls) == 1
        assert (await client.get('/api/user/places/map', params={'city':'重庆','place_id':'P1','zoom':19})).status_code == 422
        # Level 3 is the coarsest that still shows China as a country; the route page needs it.
        assert (await client.get('/api/user/places/map', params={'city':'重庆','place_id':'P1','zoom':3})).status_code == 200
        assert (await client.get('/api/user/places/map', params={'city':'重庆','place_id':'P1','zoom':2})).status_code == 422
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        with pytest.raises(BusinessError) as exc:
            await client.get('/api/user/places/map', params={'city':'重庆','place_id':'P1'})
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_provider_map_cache_and_non_image_errors():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b'\x89PNG\r\n\x1a\nfixture')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='https://example.test') as client:
        provider = AMapProvider({'enabled':True,'api_key':'test-key'},client=client)
        assert await provider.static_map(place(),13) == await provider.static_map(place(),13)
        assert len(calls) == 1
        with pytest.raises(AMapError): await provider.static_map(place(),16)
        with pytest.raises(AMapError): await provider.static_map(place(),2)
        assert await provider.static_map(place(),3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'info':'test-key invalid'})),base_url='https://example.test') as client:
        with pytest.raises(AMapError, match='未返回有效底图') as err:
            await AMapProvider({'enabled':True,'api_key':'test-key'},client=client).static_map(place(),13)
        assert 'test-key' not in str(err.value)


@pytest.mark.asyncio
async def test_modified_venue_is_verified_and_gets_entirely_new_hotels():
    service, calls = services()
    store = FakeStore(service)
    async def hotels(anchor, **kwargs):
        calls.append(('hotel',anchor.provider_place_id))
        return [HotelCandidate(provider_place_id='hotel-'+anchor.provider_place_id,name='汉庭'+anchor.name,
                location=anchor.location,distance_m=100,retrieved_at=anchor.verified_at)]
    service.travel.places.nearby_hotels = hotels
    first_place = place()
    second_place = place().model_copy(update={'provider_place_id':'P2','name':'重庆悦来国际会议中心'})
    class Places:
        configured=True
        async def verify(self, poi): return first_place if poi=='P1' else second_place
    async def submit(poi, request_id):
        fields=trip(False); fields.update(work_location='客户端名称不可信',work_location_place_id=poi)
        text,verified,_=await _prepare_chat_input(ChatRequest(input_source='quick_trip_form',trip_input=fields),Places())
        return await Supervisor(None,service,store,CONFIG).run(Scope(user_id=SCOPE.user_id,session_id=SCOPE.session_id,request_id=request_id),text,trip_input=verified)
    first=await submit('P1','map-first')
    second=await submit('P2','map-second')
    a=first['answer_document']['trip_options']; b=second['answer_document']['trip_options']
    assert a['anchor']['name']==first_place.name and b['anchor']['name']==second_place.name
    assert a['hotels'][0]['hotel']['provider_place_id']=='hotel-P1'
    assert b['hotels'][0]['hotel']['provider_place_id']=='hotel-P2'
    assert service.trip['work_location_verified']['provider_place_id']=='P2' and store.writes==2
    assert ('hotel','P1') in calls and ('hotel','P2') in calls


@pytest.mark.asyncio
async def test_excluding_hotels_does_not_require_meeting_place():
    service,calls=services(); fields=trip(False)
    fields['capability_selection']={'include':[],'exclude':['nearby_hotels']}
    from webui_new.quick_trip import build_quick_trip_message
    output=await Supervisor(None,service,FakeStore(service),CONFIG).run(SCOPE,build_quick_trip_message(fields),trip_input=fields)
    assert output['answer_document']['trip_options']['trains']
    assert all(call[0]!='hotel' for call in calls)
