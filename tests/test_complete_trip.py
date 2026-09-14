"""Regression coverage for the complete form -> evidence -> plan -> card path."""
import asyncio
import json
import os
import pytest
from agent_runtime.engine import Supervisor
from agent_runtime.profiles import PROFILES
from agent_runtime.full_trip import merge_documents
from core.presentation.answer_document import AnswerDocument, render_plain_text
from tests.test_trip_choices import services, trip
from tests.test_supervisor_runtime import FakeStore, CONFIG, SCOPE, reply, outputs
from webui_new.quick_trip import build_quick_trip_message


class CompleteTripModel:
    def __init__(self, *, policy_failure=False, slow_plan=False):
        self.roles = []
        self.policy_failure = policy_failure
        self.slow_plan = slow_plan
        self.planner_context = None

    async def __call__(self, messages, **kwargs):
        if any(t['function']['name']=='delegate' for t in kwargs['tools']):
            return reply(('prepare_trip_options', {}))
        system = messages[0]['content']
        role = next(role for role, p in PROFILES.items() if p.instructions in system)
        self.roles.append(role)
        if role == 'policy_rag':
            if self.policy_failure:
                raise OSError('test policy unavailable')
            out = outputs(messages)
            if not out:
                return reply(('search_policy', {'query': '重庆 城市等级 住宿 餐补'}))
            source = out[0]['sources'][0]
            if not source.get('already_read') and len(out) == 1:
                return reply(('read_source', {'source_id': source['source_id']}))
            return reply(('report', {'summary': '按企业制度核实适用档位。', 'evidence_refs': [source['source_id']],
                'data': {'findings': [{'item':'住宿与餐补', 'conclusion':'测试制度需核实适用档位。', 'evidence_refs':[source['source_id']]}]}}))
        if role == 'compliance':
            return reply(('report', {'status':'partial','summary':'缺少职级和实际房价，无法确定整体合规。','evidence_refs':[],
                'data':{'verdict':'unknown','checks':[]}}))
        assert role == 'trip_planner'
        assert '完整差旅的交付契约' in system
        if self.slow_plan:
            await asyncio.sleep(2)
        self.planner_context = json.loads(messages[1]['content'])
        board = next(r['data']['choices'] for r in self.planner_context['dependencies'] if r['role'] == 'travel_info')
        assert board['trains'][0]['train_no'] == 'G12'
        assert board['hotels'][0]['hotel']['name'] == '汉庭会展店'
        return reply(('report', {'status':'partial','summary':'工作时间待确认，保留真实候选。', 'missing_info':['会议开始和结束时间'],
            'data': {'itinerary': {'days': [{'date':self.planner_context['trip']['start_date'], 'activities':['G12 为去程候选；会议时间待确认。']}]}}}))


@pytest.mark.asyncio
async def test_form_without_policy_keywords_retrieves_policy_and_plans_with_real_choices():
    service, calls = services(); model = CompleteTripModel(); store = FakeStore(service); fields = trip()
    output = await Supervisor(model, service, store, CONFIG).run(SCOPE, build_quick_trip_message(fields), trip_input=fields)
    assert {'policy_rag', 'trip_planner'} <= set(model.roles)
    assert {'policy_rag', 'travel_info'} <= {r['role'] for r in model.planner_context['dependencies']}
    doc = output['answer_document']
    assert doc['trip_options']['hotels'] and doc['sources']
    assert {'差旅标准' if s['kind']=='policy' else s['title'] for s in doc['sections']} >= {'差旅标准', '行程规划'}
    assert 'G12 为去程候选' in output['response'] and '测试制度需核实适用档位' in output['response']
    assert output['outcome'] == 'partial'
    assert store.writes == 1 and any(c[0]=='hotel' for c in calls)
    checkpoint=store.rows[(SCOPE.user_id,SCOPE.request_id)]['checkpoint']
    assert 'plan-trip:references/complete-trip.md' in checkpoint['main']['skills_read']


@pytest.mark.asyncio
async def test_policy_failure_does_not_block_plan_or_destroy_candidates():
    service,_=services(); model=CompleteTripModel(policy_failure=True); fields=trip()
    result=await Supervisor(model,service,FakeStore(service),CONFIG).run(SCOPE,build_quick_trip_message(fields),trip_input=fields)
    assert result['answer_document']['trip_options']['hotels']
    assert 'trip_planner' in model.roles and result['outcome'] != 'completed'
    assert '差旅标准本次未完成' in result['response']


@pytest.mark.asyncio
async def test_explicit_compliance_request_is_not_swallowed_by_choice_tool():
    service,_=services(); model=CompleteTripModel(); fields=trip()
    result=await Supervisor(model,service,FakeStore(service),CONFIG).run(SCOPE,build_quick_trip_message(fields)+' 请检查本次出差合规。',trip_input=fields)
    assert 'compliance' in model.roles
    assert result['answer_document']['trip_options']['hotels']
    assert '无法确定整体合规' in result['response'] and result['outcome']!='completed'


@pytest.mark.asyncio
async def test_turn_timeout_preserves_map_and_verified_policy():
    service,_=services(); model=CompleteTripModel(slow_plan=True); fields=trip()
    result=await Supervisor(model,service,FakeStore(service),{**CONFIG,'turn_timeout_sec':0.3}).run(SCOPE,build_quick_trip_message(fields),trip_input=fields)
    assert result['answer_document']['trip_options']['anchor']
    assert result['answer_document']['trip_options']['hotels']
    assert result['outcome'] != 'completed'
    assert '测试制度需核实适用档位' in result['response']


def test_document_merge_keeps_itinerary_weather_sources_and_text_in_sync():
    base={'answer_document':{'title':'行程','sections':[{'kind':'trip','title':'出行选择'}]},'outcome':'completed'}
    extra={'answer_document':{'title':'资料','sections':[{'kind':'trip','goal_id':'plan','title':'每日安排','items':[{'label':'9月15日','value':'会议安排'}]},
        {'kind':'weather','title':'天气','days':[{'date':'9月15日','condition':'晴'}]}],
        'sources':[{'title':'企业制度.pdf','detail':'第3页'}],'notices':['职级待确认']}}
    result=merge_documents(merge_documents(base,extra),extra)
    doc=AnswerDocument.model_validate(result['answer_document'])
    assert len(doc.sections)==3 and len(doc.sources)==1 and doc.notices==['职级待确认']
    assert '会议安排' in result['response'] and '晴' in result['response']
    assert result['response']==doc.plain_text==render_plain_text(doc)


def test_long_itinerary_keeps_last_activity_in_card_and_plain_text():
    from agent_runtime.render import render
    from agent_runtime.contracts import Finish, SpecialistResult
    activities=['交通安排说明'*55, '住宿：会场附近酒店候选，房价待核实。']
    result=SpecialistResult(role='trip_planner',result_id='plan',task='安排行程',summary='每日安排',
        data={'itinerary':{'days':[{'date':'2026-09-15','activities':activities}]}})
    output=render(Finish(),[result])
    assert output['answer_document']['sections'][0]['items'][0]['activities']==activities
    assert activities[-1] in output['response']


@pytest.mark.skipif(os.getenv('HOMMEY_RUN_LIVE_AGENT_TESTS') != '1', reason='opt-in real providers; isolated store')
@pytest.mark.asyncio
async def test_live_complete_trip_uses_real_policy_and_planner():
    from pathlib import Path
    from time import perf_counter
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG, SUPERVISOR_CONFIG
    from agent_runtime.model_client import create_tool_model
    from agent_runtime.services import BusinessServices
    from core.integrations.travel_info import TravelInformationService
    from core.integrations.trains import create_train_query_backend
    from utils.io_executor import run_blocking
    from agent_runtime.contracts import Scope
    service,_=services()
    service.travel=TravelInformationService()
    service.trains=create_train_query_backend()
    policy=object.__new__(BusinessServices); policy.retriever=None
    async def execute(scope,name,request):
        assert name=='search_policy'
        return 'policy',await run_blocking(policy._policy,request.query)
    service.execute=execute
    snapshot=json.loads(Path('webui_new/static/design-demos/journey-flow-assets/data.json').read_text(encoding='utf-8'))
    anchor=await service.travel.places.verify(snapshot['places'][0]['place_id'])
    fields=trip(False)
    fields.update(work_location=anchor.name,work_location_verified=anchor.model_dump(mode='json'))
    scope=Scope(user_id='complete-trip-readonly-probe',session_id='complete-trip-probe',request_id='complete-trip-probe')
    store=FakeStore(service)
    model=create_tool_model(LLM_CONFIG,_generate_kwargs(LLM_CONFIG),30)
    start=perf_counter()
    result=await Supervisor(model,service,store,SUPERVISOR_CONFIG).run(scope,build_quick_trip_message(fields),trip_input=fields)
    checkpoint=store.rows[(scope.user_id,scope.request_id)]['checkpoint']
    roles={r['role']:r for r in checkpoint['results'].values()}
    audit={'elapsed_sec':round(perf_counter()-start,2),'outcome':result['outcome'],
        'roles':{role:r['status'] for role,r in roles.items()},
        'tool_calls':{c['role']:[t['function']['name'] for m in c['messages'] for t in m.get('tool_calls',[])] for c in checkpoint['children'].values()},
        'policy_queries':[json.loads(t['function']['arguments']) for c in checkpoint['children'].values() if c['role']=='policy_rag' for m in c['messages'] for t in m.get('tool_calls',[]) if t['function']['name']=='search_policy'],
        'sections':[{'kind':s['kind'],'title':s['title'],'items':len(s['items'])} for s in result['answer_document']['sections']],
        'train_count':len(result['answer_document']['trip_options']['trains']),
        'hotel_count':len(result['answer_document']['trip_options']['hotels']),
        'sources':len(result['answer_document']['sources']),
        'production_writes':0}
    print(json.dumps(audit,ensure_ascii=False),flush=True)
    output=Path('.codex-resume-work/complete-trip-live.json');output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'audit':audit,'answer_document':result['answer_document']},ensure_ascii=False,indent=2),encoding='utf-8')
    assert roles['policy_rag']['status'] in {'success','partial'}
    assert roles['trip_planner']['status'] in {'success','partial'}
    assert roles['policy_rag']['data']['findings']
    assert roles['trip_planner']['data']['itinerary']['days']
    assert audit['train_count'] and audit['hotel_count'] and audit['sources']
