import shutil
import subprocess
from pathlib import Path

pdfinfo_path = shutil.which('pdfinfo')
print('shutil.which(pdfinfo)=', pdfinfo_path)
if not pdfinfo_path:
    candidate = Path(r'C:/tools/poppler/poppler-26.02.0/Library/bin/pdfinfo.exe')
    print('candidate exists=', candidate.exists(), 'path=', candidate)
    pdfinfo_path = str(candidate) if candidate.exists() else None
if pdfinfo_path:
    out = subprocess.run([pdfinfo_path, '-v'], capture_output=True, text=True)
    print('exit', out.returncode)
    print('stdout:', out.stdout.strip())
    print('stderr:', out.stderr.strip())
else:
    print('pdfinfo not found')
