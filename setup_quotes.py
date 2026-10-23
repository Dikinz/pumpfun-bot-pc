"""Local setup for read-only quote API. Never requests wallet credentials."""
import getpass,json,sys
from pathlib import Path

def main():
    import config,exit_quotes as q
    if '--check' not in sys.argv:
        print('Jupiter API key: https://developers.jup.ag/portal')
        print('Use a data API key, NOT a wallet private key or seed phrase. Input is hidden.')
        key=getpass.getpass('Jupiter API key: ').strip()
        if not key or any(ch.isspace() for ch in key):
            print('Empty or invalid key; nothing saved.');return 1
        path=Path('.env');text=path.read_text(encoding='utf-8') if path.exists() else Path('.env.example').read_text(encoding='utf-8')
        lines=[l for l in text.splitlines() if not l.strip().startswith('JUPITER_API_KEY=')]
        path.write_text('\n'.join(lines)+'\nJUPITER_API_KEY='+json.dumps(key)+'\n',encoding='utf-8')
        config.JUPITER_API_KEY=key
        print('Saved locally in .env. Do not share that file.')
    if not config.JUPITER_API_KEY:
        print('NOT_CONFIGURED: run 02_SETTINGS.bat.');return 1
    try:
        fx=q.fetch_fx();print('QUOTE OK: SOL/USDC reference received; no transaction created.')
        data=q.request_json(config.QUOTE_RPC_URL,'METADATA',{'jsonrpc':'2.0','id':1,'method':'getTokenSupply','params':[q.SOL,{'commitment':'confirmed'}]})
        if data.get('result',{}).get('value',{}).get('decimals')!=9:
            print('METADATA_ERROR: RPC did not return expected wrapped-SOL precision.');return 1
        print('RPC OK. Ready to start 03_START.bat. This is connectivity, not a sellability guarantee.')
        return 0
    except q.ProbeError as e:
        print('CHECK FAILED:',e.status,'stage:',e.stage,'HTTP:',e.http_status or '-');return 1
    except Exception:
        print('CHECK FAILED: invalid response or local configuration.');return 1

if __name__=='__main__':raise SystemExit(main())
