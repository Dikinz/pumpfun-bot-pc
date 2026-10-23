import base64,json,struct
import pytest
import execution_verification as v
from test_execution_verification import case,MINT

def mint_account(parts):
    b=bytearray(166);b[44]=6;b[45]=1;b[165]=1
    for t,value in parts:b+=struct.pack('<HH',t,len(value))+value
    return {'owner':'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb','data':[base64.b64encode(b).decode(),'base64']}

def test_actual_sixteen_mints_metadata_only():
    d=json.load(open('research/mint_accounts_v25.json'))
    for m,a in zip(d['mints'],d['response']['result']['value']):
        x=v.decode_mint(a,6,m)
        assert x['supported'] and x['extension_types']==[18,19]

@pytest.mark.parametrize('typ',[1,9,14,65530])
def test_execution_affecting_or_unknown_extensions_blocked(case,typ):
    j,i,q,b=case;b['value'][1]=mint_account([(typ,bytes(64))])
    r=v.verify(j,i,q)
    assert r['reserves']['status']=='MINT_EXTENSIONS_UNSUPPORTED'
    assert r['reserves']['mint_evidence']['unsupported_extension_types']==[typ]
    assert r['simulation']['status']=='SKIPPED_RESERVE_GATE'

@pytest.mark.parametrize('parts',[[(18,b'x')],[(18,bytes(64)),(18,bytes(64))],[(19,bytes(79))]])
def test_malformed_metadata_rejected(parts):
    with pytest.raises(v.VerificationError,match='INVALID_MINT_EXTENSION_DATA'):v.decode_mint(mint_account(parts),6,MINT)

def test_truncated_tlv_and_wrong_account_type():
    a=mint_account([(18,bytes(64))]);raw=bytearray(base64.b64decode(a['data'][0]))
    for b in [raw[:-1],raw[:165]+bytes([2])+raw[166:]]:
        a['data'][0]=base64.b64encode(b).decode()
        with pytest.raises(v.VerificationError):v.decode_mint(a,6)

def test_metadata_passes_to_simulation_requirement(case):
    j,i,q,b=case;b['value'][1]=mint_account([(18,bytes(64))])
    r=v.verify(j,i,q)
    assert r['reserves']['status']=='RESERVES_COVER_UPPER_BOUND'
    assert r['simulation']['status']=='NOT_CONFIGURED'

def test_mint_alias_preserves_evidence_without_asserting_route(case):
    j,i,q,b=case;q['response']['routePlan'][0]['swapInfo']['ammKey']=MINT
    r=v.verify(j,i,q)
    assert r['reserves']['status']=='ROUTE_IDENTITY_UNVERIFIED'
    assert r['reserves']['route_identity']=='MINT_ALIAS_UNVERIFIED'
    assert r['reserves']['curve']==v.curve_address(MINT)
    assert r['simulation']['status']=='SKIPPED_RESERVE_GATE'
