"""Bounded traceback frames without locals, source text, URLs or arbitrary messages."""
import re,traceback
from pathlib import Path

def capture(exc):
    frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)[-12:]]
    message=str(exc)
    safe=message if isinstance(exc,TypeError) and re.fullmatch(r"[\w.<>]+\(\) got an unexpected keyword argument '[a-zA-Z_]+'.?",message) else None
    text='Traceback (sanitized; no locals or source lines):\n'+'\n'.join('  File "'+f['file']+'", line '+str(f['line'])+', in '+f['function'] for f in frames)+'\n'+type(exc).__name__+(': '+safe if safe else ': [message redacted]')
    return dict(error_type=type(exc).__name__,frames=frames,traceback=text[-4096:],safe_message=safe,message_redacted=safe is None)
