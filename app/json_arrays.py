"""Stream JSON arrays without retaining a whole monitor history in memory."""
import io,json


def write(archive,name,rows):
    encoder=json.JSONEncoder(ensure_ascii=False)
    with archive.open(name,'w',force_zip64=True) as raw,io.TextIOWrapper(raw,encoding='utf-8') as out:
        out.write('[');first=True
        for row in rows:
            if not first:out.write(',')
            first=False
            for part in encoder.iterencode(row):out.write(part)
        out.write(']')


def read(raw,chunk_size=65536):
    decoder=json.JSONDecoder();buffer='';eof=False
    text=io.TextIOWrapper(raw,encoding='utf-8')
    def more():
        nonlocal buffer,eof
        part=text.read(max(chunk_size,min(65536,len(buffer) or 1)))
        if not part:eof=True
        buffer+=part
    def trim():
        nonlocal buffer
        buffer=buffer.lstrip()
        while not buffer and not eof:more();buffer=buffer.lstrip()
    try:
        trim()
        if not buffer.startswith('['):raise ValueError('JSON_ARRAY_REQUIRED')
        buffer=buffer[1:];trim()
        if buffer.startswith(']'):buffer=buffer[1:]
        else:
            while True:
                while True:
                    try:
                        value,end=decoder.raw_decode(buffer)
                        if end==len(buffer) and not eof:more();continue
                        if end<len(buffer) and buffer[end] not in ',] \t\r\n':
                            if not eof:more();continue
                            raise ValueError('JSON_ARRAY_VALUE')
                        break
                    except json.JSONDecodeError:
                        if eof:raise
                        more()
                buffer=buffer[end:];yield value;trim()
                if buffer.startswith(']'):buffer=buffer[1:];break
                if not buffer.startswith(','):raise ValueError('JSON_ARRAY_DELIMITER')
                buffer=buffer[1:];trim()
                if buffer.startswith(']'):raise ValueError('JSON_ARRAY_TRAILING_COMMA')
        # Consume and validate the tail too, including empty arrays.
        while True:
            if buffer.strip():raise ValueError('JSON_ARRAY_TRAILING_DATA')
            if eof:break
            buffer='';more()
    finally:text.close()
