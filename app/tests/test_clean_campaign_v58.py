import json,sqlite3,hashlib,os,socket,subprocess,sys,time,urllib.request
from pathlib import Path
import pytest
import clean_campaign as campaign,campaign_view as view,config,database as db,main,paper_branch
from test_primary_integrity import fresh

def test_clean_start_zero_positions_both_scenarios_and_resume(fresh):
    campaign.preflight(config.DB);m=campaign.ensure(fresh,config.DB)
    overview=view.overview(config.DB)
    assert overview['primary']['entries']==0 and overview['primary']['positions']==[]
    assert overview['primary']['capital']['cash']==30 and overview['amm'] is None
    target=paper_branch.seed(config.DB)
    overview=view.overview(config.DB)
    assert overview['amm']['entries']==0 and overview['amm']['capital']['cash']==30
    assert overview['amm']['campaign']['id']==m['id']
    assert main.buy('new-position',1)[0]
    before=[tuple(x) for x in fresh.execute('SELECT * FROM trades')]
    campaign.preflight(config.DB)
    assert campaign.ensure(fresh,config.DB)['id']==m['id']
    assert before==[tuple(x) for x in fresh.execute('SELECT * FROM trades')]
    assert fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1
    assert Path(target).exists()

def test_old_financial_data_rejected_without_mutation(fresh):
    assert main.buy('old-position',1)[0]
    path=Path(config.DB);before=path.read_bytes()
    with pytest.raises(campaign.CampaignError,match='LEGACY_TEST_DATA_BLOCKED'):campaign.preflight(path)
    with pytest.raises(campaign.CampaignError,match='LEGACY_TEST_DATA_BLOCKED'):campaign.ensure(fresh,path)
    assert path.read_bytes()==before and fresh.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1

def test_orphan_branch_rejected(tmp_path):
    primary=tmp_path/'clean.db';paper_branch.path(primary).touch()
    with pytest.raises(campaign.CampaignError,match='UNBOUND_BRANCH'):campaign.preflight(primary)
    assert not primary.exists()

def test_import_only_settings_keeps_filters_not_database_or_wallet(tmp_path):
    source=tmp_path/'old.env';target=tmp_path/'new.env'
    source.write_text('DATABASE_PATH=old.db\nSTART_BALANCE=100\nEXECUTION_MODE=live\nSTOP_LOSS_PCT=-0.12\nMAX_HOLD_SECONDS=120\nJUPITER_API_KEY=data-api-secret\nQUOTE_RPC_URL=https://rpc.example/key\nWALLET_PRIVATE_KEY=do-not-copy\nUNKNOWN_VAR=no\n')
    before=source.read_bytes();target.write_text('DATABASE_PATH=data/clean_0762.db\n')
    assert campaign.import_settings(source,target)>=4
    from dotenv import dotenv_values
    d=dotenv_values(target)
    assert d['DATABASE_PATH']=='data/clean_0762.db' and d['START_BALANCE']=='30' and d['EXECUTION_MODE']=='paper'
    assert d['STOP_LOSS_PCT']=='-0.12' and d['MAX_HOLD_SECONDS']=='120' and d['JUPITER_API_KEY']=='data-api-secret'
    assert 'WALLET_PRIVATE_KEY' not in d and 'UNKNOWN_VAR' not in d and source.read_bytes()==before

def test_campaign_mismatch_blocks_comparison(fresh):
    campaign.ensure(fresh,config.DB);target=paper_branch.seed(config.DB)
    with sqlite3.connect(target) as c:
        marker=campaign.marker(c);marker['id']='a'*32;c.execute('UPDATE meta SET v=? WHERE k=?',(json.dumps(marker),campaign.KEY))
    result=view.overview(config.DB)
    assert result['amm']['status']=='CAMPAIGN_MISMATCH' and result['roadmap'][0]['status']=='IN_PROGRESS'
    assert result['alerts']

def test_overview_financial_accounting_and_slot_warning(fresh):
    import paper_capital as cap
    campaign.ensure(fresh,config.DB);assert main.buy('m',1)[0];tid=main.P['m']['trade_id']
    cap.record(fresh,tid,'BUY',3,{'pool_fee_rate':.01});main.sell('m',1.1,'TEST');cap.record(fresh,tid,'SELL',3.3,{'pool_fee_rate':.01})
    result=view.overview(config.DB)['primary']
    costs=fresh.execute('SELECT SUM(total_cost) FROM capital_ledger_v0721').fetchone()[0]
    assert result['net']==pytest.approx(.3-costs) and result['closed']==1 and result['entries']==1
    assert result['trades'][0]['cost']==pytest.approx(costs)

def test_fresh_marker_does_not_allow_auto_live_or_goal_pass(fresh):
    campaign.ensure(fresh,config.DB);o=view.overview(config.DB)
    assert o['roadmap'][0]['status']=='IN_PROGRESS'
    assert o['roadmap'][2]['status']=='NOT_TESTED' and o['roadmap'][3]['status']=='BLOCKED'
    assert config.EXECUTION_MODE=='paper'

def test_front_page_and_light_api_separate_from_engineering(tmp_path):
    app=Path(__file__).parents[1]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=dict(os.environ,DATABASE_PATH=str(tmp_path/'ui.db'),DASHBOARD_PORT=str(port));env.pop('PYTHONPATH',None)
    with (tmp_path/'server.log').open('w+') as log:
        child=subprocess.Popen([sys.executable,str(app/'dashboard/server.py')],cwd=app,env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/overview',timeout=.5) as r:o=json.load(r)
                    break
                except OSError:time.sleep(.1)
            else:log.seek(0);raise AssertionError(log.read())
            assert o['primary']['positions']==[] and o['version']=='v0.7.101.7'
            # v0.7.101.4: the trading terminal is the main page; the summary page is /classic.
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as r:main_page=r.read().decode()
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/terminal') as r:assert r.read().decode()==main_page
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/classic') as r:page=r.read().decode()
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/details') as r:detail=r.read().decode()
            assert 'Дорожная карта' in page and "fetch('/overview'" in page
            assert 'timelinePending' not in page and 'timelinePending' in detail
            assert len(json.dumps(o))<20000
        finally:child.terminate();child.wait(timeout=5)

def test_corrupt_branch_does_not_hide_primary(fresh):
    campaign.ensure(fresh,config.DB);paper_branch.path(config.DB).write_bytes(b'not a sqlite file')
    result=view.overview(config.DB)
    assert result['primary']['entries']==0 and result['amm']['status']=='BRANCH_REPORT_ERROR'
    assert result['alerts']

def test_roadmap_counts_do_not_replace_mechanics_or_live_checks():
    p=dict(runtime=dict(state='STOPPED'),capital=dict(status='OK'),positions=[])
    a=dict(role='amm',closed=20,amm_exits=1,runtime=dict(state='STOPPED'),transport=dict(status='STOPPED',exit_code=0,forced=False),capital=dict(status='OK'),accounting_missing=0,positions=[],input_gap=False,net=1)
    assert view.roadmap(p,a)[0]['status']=='IN_PROGRESS'
    assert view.roadmap(p,dict(a,amm_exits=0))[0]['status']=='IN_PROGRESS'
    assert view.roadmap(p,dict(a,positions=[dict(mint='open')]))[0]['status']=='IN_PROGRESS'
    assert view.roadmap(p,dict(a,input_gap=True))[0]['status']=='IN_PROGRESS'
    assert view.roadmap(p,a)[3]['status']=='BLOCKED'
