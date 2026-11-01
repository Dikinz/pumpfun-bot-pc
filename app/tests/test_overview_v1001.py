"""Keep the existing light API size contract, including Windows path overhead."""
import json
import pytest
import config,campaign_view as view,system_health
from test_primary_integrity import fresh


def test_overview1001_criteria_once_preserves_roadmap_evidence(fresh):
    data=view.overview(config.DB)
    full=data['roadmap'];compact=data['roadmap_progress']['stages']
    assert len(full)==len(compact)==4
    for stage,card in zip(full,compact):
        assert stage['criterion']
        assert card=={k:v for k,v in stage.items() if k!='criterion'}
    assert compact[0]['checkpoints']==full[0]['checkpoints']
    assert data['roadmap_progress']['collection_basis_v100']=='PRIMARY_NEW_CLOSED'
    # Same serializer as the existing failing Windows test, not UTF-8 bytes.
    assert len(json.dumps(data))<18000


@pytest.mark.parametrize('user',['OMG','Пользователь'])
def test_overview1001_windows_paths_keep_light_payload(fresh,monkeypatch,user):
    path='C:\\Users\\'+user+'\\Desktop\\pumpfun_bot_v0.7.101.1_overview\\app\\data\\clean_0762.db'
    resources=dict(database=path,processes=[dict(pid=100+i,role=role,database=path,executable='C:\\Users\\'+user+'\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe') for i,role in enumerate(('primary','monitor','dashboard'))])
    monkeypatch.setattr(system_health,'read_state',lambda primary:resources)
    before=list(fresh.iterdump())
    data=view.overview(config.DB)
    assert data['resources']==resources
    assert data['primary']['positions']==[] and data['primary']['capital']['principal']==0
    assert len(json.dumps(data))<18000
    assert list(fresh.iterdump())==before
