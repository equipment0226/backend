"""Launch the API and any bundled frontends after local GPU preparation."""
import os
import json
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]


def load_env():
    path=ROOT/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line=line.strip()
            if line and not line.startswith('#') and '=' in line:
                key,value=line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))


def ensure_local_gpu():
    """Opt-in GPU validation before starting services; preserve resident work."""
    config_path = ROOT / '.local' / 'ollama-gpu' / 'config.json'
    if os.name != 'nt' or not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise RuntimeError('Could not read the local GPU startup configuration.') from error
    if not isinstance(config, dict) or not isinstance(config.get('enabled', False), bool):
        raise RuntimeError('Local GPU configuration must be an object with a boolean enabled setting.')
    if not config.get('enabled', False):
        return
    try:
        endpoint = urlsplit(os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434'))
        port = endpoint.port or 11434
        if (endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', 'localhost'}
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.path not in {'', '/'} or endpoint.query or endpoint.fragment
                or endpoint.port == 0):
            raise ValueError
    except ValueError as error:
        raise RuntimeError('GPU startup requires a local HTTP OLLAMA_BASE_URL on 127.0.0.1 or localhost.') from error
    powershell = shutil.which('powershell.exe') or shutil.which('pwsh.exe')
    if not powershell:
        raise RuntimeError('PowerShell is required for local GPU startup.')
    command = [powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(ROOT / 'scripts' / 'start-ollama-gpu.ps1'), '-Port', str(port),
               '-Model', os.environ.get('OLLAMA_MODEL', 'llama3:latest'),
               '-PythonExecutable', sys.executable]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError('Local GPU validation failed. API and frontend services were not started.')


def main():
    load_env()
    try:
        ensure_local_gpu()
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    profile_path = ROOT / '.local' / 'start-profile.json'
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding='utf-8'))
        if profile.get('profile') == 'fresh':
            os.environ.setdefault('DEBTOFF_START_PROFILE', 'fresh')
    os.environ.setdefault('DEBTOFF_LEGAL_WATCH', '1')
    processes=[]
    commands=[['-m','uvicorn','apps.api.main:app','--host','127.0.0.1','--port','8000']]
    addresses=['API: http://localhost:8000/docs']
    # The backend export has no frontend files. The same launcher remains
    # usable there without attempting to start the integrated workspace pages.
    if (ROOT / 'scripts' / 'serve_frontend.py').is_file():
        for app, label, port in (('portal', 'Portal', 5173), ('office', 'Office', 5174)):
            if (ROOT / 'apps' / app / 'index.html').is_file():
                commands.append(['scripts/serve_frontend.py', app, '--port', str(port)])
                addresses.append(f'{label}: http://localhost:{port}')
    try:
        for command in commands:
            processes.append(subprocess.Popen([sys.executable,*command],cwd=ROOT))
        print(' | '.join(addresses),flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__=='__main__':
    raise SystemExit(main())
