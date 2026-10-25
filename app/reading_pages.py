"""Bounded, cancellable PDF range extraction; no networking or OCR."""
import asyncio
import json
from pathlib import Path
import sys


def extract(path, start, end, deep=False):
    from pypdf import PdfReader
    if not (1 <= start <= end and end - start < 100):
        raise ValueError('Invalid page range')
    reader = PdfReader(path)
    total = len(reader.pages)
    end = min(end, total)
    units, empty, used, last = [], [], 0, start - 1
    for page in range(start, end + 1):
        text = reader.pages[page - 1].extract_text() or ''
        # Never truncate a deep page silently. Oversized pages require manual handling.
        if deep and len(text) > 240000:
            raise ValueError('Page text exceeds safe limit')
        if not deep and used + len(text) > 240000 and units:
            break
        if not text.strip():
            empty.append(page)
        else:
            text = text[:240000] if not deep else text
            units.append({'label': f'第{page}页', 'text': text})
            used += len(text)
        last = page
    return {'units': units, 'empty': empty, 'total': total, 'start': start, 'end': last,
            'kind': 'PDF文字；未识别图表画面', 'truncated': not deep}


async def read(path, start, end, deep=False):
    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
        str(path), str(start), str(end), 'deep' if deep else 'sample',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 120)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode or len(stdout) > 4000000:
        raise ValueError('PDF range extraction failed')
    result = json.loads(stdout)
    if 'error' in result:
        raise ValueError('PDF page could not be extracted')
    return result


if __name__ == '__main__':
    try:
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        result = extract(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == 'deep')
    except Exception:
        result = {'error': 'PDF page extraction failed'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
