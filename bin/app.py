"""Tala Render — server web render (Blender + RTX 5090).

Satu proses: web UI, login, upload, analisis file, antrian, worker render, statistik server.
Jalan sebagai LOCAL SERVICE di http://<server>:8080
"""
import datetime
import html
import http.cookies
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BIN)
import users  # noqa: E402

ROOT = r'D:\RenderServer'
APP = os.path.join(ROOT, 'app')
BLENDER = os.path.join(APP, 'blender', 'blender.exe')
FFMPEG = os.path.join(APP, 'tools', 'ffmpeg-windows-amd64.exe')
NVSMI = r'C:\Windows\System32\nvidia-smi.exe'
FONTS = os.path.join(APP, 'blender', '5.2', 'datafiles', 'fonts')
WEB = os.path.join(APP, 'web')
DATA = os.path.join(ROOT, 'data')
JOBS_DIR = os.path.join(DATA, 'jobs')
UPLOADS = os.path.join(DATA, 'uploads')
OUT_ROOT = os.path.join(ROOT, 'Share', 'Output')
PORT = 8080
MAX_UPLOAD = 5 * 1024 ** 3
SESSION_IDLE_DAYS = 90         # sesi hanya habis bila tidak dibuka sama sekali selama 90 hari
COOKIE_MAX_AGE = 400 * 24 * 3600  # batas maksimum cookie di browser
SESSIONS_FILE = None             # diisi setelah DATA dibuat
MAX_FAILS, LOCK_SECS = 5, 300
NO_WINDOW = 0x08000000

for d in (JOBS_DIR, UPLOADS, OUT_ROOT):
    os.makedirs(d, exist_ok=True)
SESSIONS_FILE = os.path.join(DATA, 'sessions.json')


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def safe_name(name, fallback='render'):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', name).strip(' ._')
    return (s or fallback)[:80]


SERVER_HOST = '192.168.110.152'   # IP statis LAN; dipakai untuk alamat folder share (lebih andal dari nama PC)


def server_name():
    return SERVER_HOST


# ======================================================================= state
lock = threading.RLock()
sessions = {}           # sha256(token) -> {'user', 'created', 'last'}  (disimpan ke disk, bertahan saat restart)
fails = {}              # ip -> (count, t0)
jobs = {}               # id -> job dict
uploads = {}            # id -> meta
procs = {}              # job id -> Popen
stats = {'gpu': None, 'disk': None, 'ok': True, 'updated': 0}
wake = threading.Event()


def tok_key(tok):
    import hashlib
    return hashlib.sha256(tok.encode('utf-8')).hexdigest()


def atomic_write_json(path, obj, indent=None):
    """Tulis JSON secara atomik. Di Windows, os.replace bisa gagal sesaat (WinError 5/32) bila
    antivirus/indexer sedang membuka file tujuan -> ulangi beberapa kali, jangan pernah menggagalkan job."""
    tmp = '%s.%d.tmp' % (path, threading.get_ident())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=indent)
        for i in range(25):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.05 * (i + 1))
        log('PERINGATAN: gagal menyimpan %s (file sedang dikunci program lain)' % os.path.basename(path))
    except Exception as e:
        log('PERINGATAN: gagal menyimpan %s: %s' % (os.path.basename(path), e))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def save_sessions():
    atomic_write_json(SESSIONS_FILE, sessions)


def load_sessions():
    try:
        with open(SESSIONS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return
    cutoff = time.time() - SESSION_IDLE_DAYS * 86400
    sessions.update({k: v for k, v in data.items() if isinstance(v, dict) and v.get('last', 0) > cutoff})


def save_job(j):
    atomic_write_json(os.path.join(JOBS_DIR, j['id'] + '.json'), j, indent=1)


def load_jobs():
    for fn in os.listdir(JOBS_DIR):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(JOBS_DIR, fn), encoding='utf-8') as f:
                j = json.load(f)
        except Exception:
            continue
        if j['status'] == 'running':          # server mati saat render -> ulang
            j['status'] = 'queued'
            j['progress'] = new_progress(j)
            j['results'] = []
            j['note'] = 'Restarted automatically after a server restart'
            save_job(j)
        jobs[j['id']] = j


def new_progress(j):
    if j['mode'] == 'image':
        total = len(j['cameras'])
    else:
        total = len(j['cameras']) * (j['frame_end'] - j['frame_start'] + 1)
    return {'unit_done': 0, 'unit_total': total, 'cam_i': 0, 'cam': None, 'frame': None,
            'sample': 0, 'sample_total': 0, 'blender_remaining': None,
            'unit_times': [], 'unit_started': None, 'phase': 'antri', 'preview': None}


# ======================================================================= stats
def stats_loop():
    while True:
        g = None
        try:
            out = subprocess.run([NVSMI, '--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu',
                                  '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
            name, util, used, total, temp = [x.strip() for x in out.split(',')]
            g = {'name': name.replace('NVIDIA GeForce ', ''), 'util': int(util), 'mem_used': int(used),
                 'mem_total': int(total), 'temp': int(temp)}
        except Exception:
            g = None
        try:
            du = shutil.disk_usage('D:\\')
            d = {'free': du.free, 'total': du.total}
        except Exception:
            d = None
        with lock:
            stats.update(gpu=g, disk=d, ok=g is not None, updated=time.time())
        time.sleep(3)


# ======================================================================= analyse
def analyse(blend, search_root):
    p = subprocess.run([BLENDER, '-b', '--factory-startup', '--disable-autoexec', blend,
                        '--python', os.path.join(BIN, 'inspect_blend.py')],
                       capture_output=True, text=True, timeout=600, errors='replace', creationflags=NO_WINDOW,
                       env=dict(os.environ, TR_SEARCH=search_root))
    for line in p.stdout.splitlines():
        if line.startswith('@@INFO@@'):
            return json.loads(line[8:])
    tail = [l for l in (p.stdout + p.stderr).splitlines() if 'rror' in l or 'not supported' in l][-3:]
    raise RuntimeError('Blender could not open this file. ' + ' '.join(tail)[:300])


def find_blends(folder):
    found = []
    for root, _, files in os.walk(folder):
        found += [os.path.join(root, f) for f in files if f.lower().endswith('.blend')]
    if not found:
        return []
    depth = min(p.count(os.sep) for p in found)
    return sorted(p for p in found if p.count(os.sep) == depth)


# ======================================================================= worker
SAMPLE_RE = re.compile(r'Sample (\d+)/(\d+)')
FRA_RE = re.compile(r'Fra:\s*(\d+)')
REM_RE = re.compile(r'Remaining:\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)')
SAVED_RE = re.compile(r"Saved: '(.+)'")
ERR_HINTS = [
    ('out of memory', 'Not enough GPU/RAM memory. Try a lower resolution or fewer samples.'),
    ('cuda error', 'GPU error while rendering.'),
    ('optix error', 'GPU error while rendering.'),
    ('exception_access_violation', 'Blender crashed while rendering this file.'),
    ('cannot read file', 'The .blend file could not be read.'),
    ('file format is not supported', 'The .blend file could not be read.'),
]


def thumb(src, dst, width=960):
    try:
        subprocess.run([FFMPEG, '-v', 'error', '-y', '-i', src, '-vf', f"scale='min({width},iw)':-2",
                        '-frames:v', '1', '-q:v', '3', dst], timeout=60, creationflags=NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(dst)
    except Exception:
        return False


def job_thumb_dir(j):
    d = os.path.join(JOBS_DIR, j['id'] + '_thumbs')
    os.makedirs(d, exist_ok=True)
    return d


def set_preview_async(j, src):
    def run():
        dst = os.path.join(job_thumb_dir(j), 'preview.jpg')
        tmp = dst + '.tmp.jpg'
        if thumb(src, tmp):
            os.replace(tmp, dst)
            with lock:
                j['progress']['preview'] = int(time.time())
    threading.Thread(target=run, daemon=True).start()


def make_result_thumb_async(j, res, src):
    """Thumbnail hasil + preview dibuat di thread terpisah (tidak menahan server)."""
    def run():
        tn = os.path.join(job_thumb_dir(j), res['file'] + '.jpg')
        if thumb(src, tn):
            with lock:
                res['thumb'] = os.path.basename(tn)
            pv = os.path.join(job_thumb_dir(j), 'preview.jpg')
            shutil.copyfile(tn, pv + '.tmp')
            os.replace(pv + '.tmp', pv)
            with lock:
                j['progress']['preview'] = int(time.time())
                save_job(j)
    threading.Thread(target=run, daemon=True).start()


def build_args(j):
    expr = "import sys; sys.path.insert(0, r'%s'); import render_job as t; t." % BIN
    a = [BLENDER, '-b', '--disable-autoexec', '--python', os.path.join(BIN, 'force_gpu.py'),
         j['blend'], '--python', os.path.join(BIN, 'render_job.py')]
    for i in range(len(j['cameras'])):
        a += ['--python-expr', expr + 'cam_start(%d)' % i]
        if j['mode'] == 'image':
            cam = j['cameras'][i]
            a += ['-f', str(j.get('cam_frames', {}).get(cam, j['frame']))]
        else:
            a += ['-s', str(j['frame_start']), '-e', str(j['frame_end']), '-a']
        a += ['--python-expr', expr + 'cam_done(%d)' % i]
    a += ['--python-expr', expr + 'end()']
    return a


def unit_finished(j):
    p = j['progress']
    t = time.time()
    if p['unit_started']:
        p['unit_times'].append(round(t - p['unit_started'], 2))
        p['unit_times'] = p['unit_times'][-50:]
    p['unit_done'] += 1
    p['unit_started'] = t
    p['sample'] = 0
    p['blender_remaining'] = None


def run_job(j):
    job_file = os.path.join(JOBS_DIR, j['id'] + '.json')
    os.makedirs(j['out_dir'], exist_ok=True)
    with lock:
        j['status'] = 'running'
        j['started'] = now_iso()
        j['progress'] = new_progress(j)
        j['progress']['phase'] = 'membuka file'
        j['progress']['unit_started'] = time.time()
        j['results'] = []
        j['log'] = []
        save_job(j)
    env = dict(os.environ, TR_JOB=job_file, TR_SEARCH=os.path.join(JOBS_DIR, j['id'] + '_src'))
    t0 = time.time()
    proc = subprocess.Popen(build_args(j), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, errors='replace', bufsize=1, creationflags=NO_WINDOW)
    with lock:
        procs[j['id']] = proc
    error_hint, ended, last_lines, last_save = None, False, [], 0
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        last_lines = (last_lines + [line])[-40:]
        low = line.lower()
        with lock:
            p = j['progress']
            if line.startswith('@@'):
                tag, _, payload = line[2:].partition('@@')
                data = json.loads(payload or '{}')
                if tag == 'RELINK':
                    j['missing'] = data.get('missing', [])
                    if data.get('relinked'):
                        j['log'].append('%s  %d file(s) relinked from the upload' % (time.strftime('%H:%M:%S'), data['relinked']))
                    if j['missing']:
                        j['log'].append('%s  MISSING: %s' % (time.strftime('%H:%M:%S'), ', '.join(j['missing'][:20])))
                elif tag == 'START':
                    j['out_w'], j['out_h'] = data['w'], data['h']
                    j['engine'] = data['engine']
                    j['samples_eff'] = data['samples']
                    j['ext'] = data['ext']
                    p['phase'] = 'render'
                elif tag == 'CAM_START':
                    p['cam'], p['cam_i'] = data['name'], data['i']
                    p['unit_started'] = time.time()
                    j['log'].append('%s  started camera %s' % (time.strftime('%H:%M:%S'), data['name']))
                elif tag == 'CAM_DONE':
                    cam = data['name']
                    if j['mode'] == 'image':
                        f = os.path.join(j['out_dir'], data['file'])
                        if data.get('ok') and os.path.exists(f):
                            res = {'cam': cam, 'file': data['file'], 'size': os.path.getsize(f), 'kind': 'image'}
                            j['results'].append(res)
                            make_result_thumb_async(j, res, f)
                        unit_finished(j)
                    j['log'].append('%s  finished camera %s' % (time.strftime('%H:%M:%S'), cam))
                elif tag == 'END':
                    ended = True
            else:
                m = SAMPLE_RE.search(line)
                if m:
                    p['sample'], p['sample_total'] = int(m.group(1)), int(m.group(2))
                    p['status_text'] = None
                elif '|' in line and FRA_RE.search(line):
                    # tahap persiapan Blender: "Synchronizing object | Pillow", "Loading render kernels" ...
                    segs = [x.strip() for x in line.split('|')[1:]]
                    segs = [x for x in segs if x and not re.match(r'(Mem|Time|Remaining|Fra|Peak)\b', x)]
                    txt = ' · '.join(segs[-2:])
                    if txt and 'Finished' not in txt and len(txt) < 140:
                        if p.get('status_text') is None or p.get('sample', 0) == 0:
                            p['status_text'] = txt
                            if not p.get('status_since') or p.get('sample'):
                                p['status_since'] = time.time()
                m = FRA_RE.search(line)
                if m:
                    p['frame'] = int(m.group(1))
                m = REM_RE.search(line)
                if m:
                    h, mi, se = m.groups()
                    p['blender_remaining'] = int(h or 0) * 3600 + int(mi) * 60 + float(se)
                m = SAVED_RE.search(line)
                if m and j['mode'] == 'anim':
                    unit_finished(j)
                    if time.time() - last_save > 4:
                        last_save = time.time()
                        set_preview_async(j, m.group(1))
                for key, hint in ERR_HINTS:
                    if key in low:
                        error_hint = hint
            if time.time() - t0 > 2:
                save_job(j)
                t0 = time.time()
    rc = proc.wait()
    with lock:
        procs.pop(j['id'], None)
        canceled = j['status'] == 'canceling'
    # ---- hasil animasi per kamera (frame yang sudah jadi selalu ditampilkan; video hanya jika selesai)
    if j['mode'] == 'anim':
        complete = not canceled and ended
        if complete:
            with lock:
                j['progress']['phase'] = 'membuat video'
        for cam in j['cameras']:
            safe = j['cam_files'][cam]
            fdir = os.path.join(j['out_dir'], safe, 'frames')
            frames = sorted(f for f in os.listdir(fdir)) if os.path.isdir(fdir) else []
            if not frames:
                continue
            res = {'cam': cam, 'file': safe, 'kind': 'frames', 'frames': len(frames),
                   'size': sum(os.path.getsize(os.path.join(fdir, f)) for f in frames)}
            if frames:
                tn = os.path.join(job_thumb_dir(j), safe + '.jpg')
                thumb(os.path.join(fdir, frames[len(frames) // 2]), tn)
                res['thumb'] = os.path.basename(tn)
            ext = os.path.splitext(frames[0])[1].lower() if frames else ''
            if complete and j.get('video') and frames and ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'):
                mp4 = os.path.join(j['out_dir'], safe + '.mp4')
                subprocess.run([FFMPEG, '-v', 'error', '-y', '-framerate', str(j['fps']),
                                '-start_number', str(j['frame_start']), '-i', os.path.join(fdir, '%04d' + ext),
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18',
                                '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', mp4],
                               timeout=7200, creationflags=NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(mp4):
                    res['video'] = os.path.basename(mp4)
                    res['video_size'] = os.path.getsize(mp4)
            with lock:
                j['results'].append(res)
    with lock:
        j['finished'] = now_iso()
        if canceled:
            j['status'] = 'canceled'
        elif ended and rc == 0:
            j['status'] = 'done'
        else:
            j['status'] = 'failed'
            j['error'] = error_hint or 'Render stopped unexpectedly (exit code %s).' % rc
            j['log'] += ['--- last Blender output ---'] + last_lines[-12:]
        j['progress']['phase'] = j['status']
        save_job(j)
    log('job %s %s (%s) -> %s' % (j['id'], j['title'], j['user'], j['status']))


def worker_loop():
    while True:
        nxt = None
        with lock:
            q = sorted((x for x in jobs.values() if x['status'] == 'queued'), key=lambda x: x['created'])
            nxt = q[0] if q else None
        if nxt is None:
            wake.wait(5)
            wake.clear()
            continue
        try:
            run_job(nxt)
        except Exception as e:
            log('worker error: ' + traceback.format_exc())
            with lock:
                proc = procs.pop(nxt['id'], None)
            if proc and proc.poll() is None:      # jangan tinggalkan Blender yatim yang masih memakai GPU
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], creationflags=NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with lock:
                nxt['status'] = 'failed'
                nxt['error'] = 'Server error: %s' % e
                nxt['finished'] = now_iso()
                save_job(nxt)


def cleanup_loop():
    while True:
        cutoff = time.time() - 24 * 3600
        for d in os.listdir(UPLOADS):
            p = os.path.join(UPLOADS, d)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    with lock:
                        uploads.pop(d, None)
            except OSError:
                pass
        time.sleep(3600)


# ======================================================================= views
def eta_info(j):
    p = j['progress']
    if j['status'] != 'running':
        return None
    done, total = p['unit_done'], p['unit_total']
    times = p['unit_times']
    cur_frac = (p['sample'] / p['sample_total']) if p['sample_total'] else 0
    pct = min(100.0, 100.0 * (done + cur_frac) / total) if total else 0
    out = {'pct': round(pct, 1), 'cur_frac': round(cur_frac, 3), 'measured': bool(times)}
    if times:
        avg = sum(times[-10:]) / len(times[-10:])
        elapsed_cur = time.time() - (p['unit_started'] or time.time())
        remaining = max(0, avg * (total - done) - elapsed_cur)
        out.update(avg_unit=round(avg, 1), remaining=round(remaining),
                   finish=(datetime.datetime.now() + datetime.timedelta(seconds=remaining)).strftime('%H:%M'))
    if p['blender_remaining'] is not None:
        out['cur_remaining'] = round(p['blender_remaining'])
    return out


def job_public(j, full=False):
    keys = ['id', 'user', 'title', 'created', 'started', 'finished', 'status', 'mode', 'cameras', 'res',
            'out_w', 'out_h', 'engine', 'samples_eff', 'frame', 'frame_start', 'frame_end', 'error', 'note',
            'src_name', 'format', 'video', 'missing']
    d = {k: j.get(k) for k in keys}
    p = j['progress']
    d['progress'] = {k: p.get(k) for k in ('unit_done', 'unit_total', 'cam', 'cam_i', 'frame', 'sample',
                                            'sample_total', 'phase', 'preview', 'status_text')}
    if p.get('unit_started') and j['status'] == 'running':
        d['progress']['unit_elapsed'] = round(time.time() - p['unit_started'])
    d['eta'] = eta_info(j)
    d['results'] = j.get('results', [])
    d['n_results'] = len(d['results'])
    d['out_size'] = sum(r.get('size', 0) + r.get('video_size', 0) for r in d['results'])
    if full:
        d['unc'] = '\\\\%s\\Render\\Output\\%s\\%s' % (server_name(), j['user'], os.path.basename(j['out_dir']))
        d['smb'] = 'smb://%s/Render/Output/%s/%s' % (server_name(), urllib.parse.quote(j['user']),
                                                    urllib.parse.quote(os.path.basename(j['out_dir'])))
        d['log'] = j.get('log', [])[-60:]
    return d


def state_for(user, role):
    with lock:
        allj = sorted(jobs.values(), key=lambda x: x['created'])
        active = [x for x in allj if x['status'] in ('running', 'canceling')]
        queued = [x for x in allj if x['status'] == 'queued']
        recent = sorted((x for x in allj if x['status'] in ('done', 'failed', 'canceled')),
                        key=lambda x: x.get('finished') or '', reverse=True)[:6]
        s = dict(stats)
    if not s['ok']:
        status = {'level': 'bad', 'text': 'GPU not detected — contact IT'}
    elif active:
        status = {'level': 'busy', 'text': 'Server busy — 1 rendering' + (', %d waiting' % len(queued) if queued else '')}
    else:
        status = {'level': 'ok', 'text': 'Server ready — queue empty' if not queued else 'Server ready'}
    return {'me': {'user': user, 'role': role}, 'server': {'gpu': s['gpu'], 'disk': s['disk'], 'status': status},
            'active': [job_public(x) for x in active], 'queued': [job_public(x) for x in queued],
            'recent': [job_public(x) for x in recent]}


# ======================================================================= http
LOGIN_HTML = open(os.path.join(WEB, 'login.html'), encoding='utf-8').read()


class H(BaseHTTPRequestHandler):
    server_version = 'TalaRender'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers
    def send(self, code, body=b'', ctype='text/html; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj), 'application/json; charset=utf-8')

    def redirect(self, to, headers=None):
        h = {'Location': to}
        h.update(headers or {})
        self.send(303, b'', headers=h)

    def session(self):
        c = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        tok = c['tr_session'].value if 'tr_session' in c else None
        if not tok:
            return None, None
        key = tok_key(tok)
        with lock:
            s = sessions.get(key)
            if not s:
                return None, None
            now = time.time()
            if now - s['last'] > SESSION_IDLE_DAYS * 86400:
                sessions.pop(key, None)
                save_sessions()
                return None, None
            info = users.load()['users'].get(s['user'])
            if not info:                      # akun dihapus -> sesi tidak berlaku
                sessions.pop(key, None)
                save_sessions()
                return None, None
            if now - s['last'] > 600:         # catat aktivitas (maks. tiap 10 menit)
                s['last'] = now
                save_sessions()
            return key, (s['user'], info['role'], None)

    def read_json(self):
        n = min(int(self.headers.get('Content-Length', 0) or 0), 1024 * 1024)
        return json.loads(self.rfile.read(n) or b'{}')

    def same_origin(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == self.headers.get('Host')

    def send_file(self, path, ctype=None, download_name=None, cache=False):
        if not os.path.isfile(path):
            return self.send(404, 'Not found')
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        ctype = ctype or {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.mp4': 'video/mp4',
                          '.exr': 'application/octet-stream', '.woff2': 'font/woff2'}.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'public, max-age=86400' if cache else 'no-cache')
        if download_name:
            q = urllib.parse.quote(download_name)
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % q)
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(path, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def login_page(self, err='', user='', code=200):
        page = LOGIN_HTML.replace('__ERR__', '<div class="err">%s</div>' % html.escape(err) if err else '')
        self.send(code, page.replace('__USER__', html.escape(user)))

    # ---------- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        if path == '/static/inter.woff2':
            return self.send_file(os.path.join(FONTS, 'Inter.woff2'), cache=True)
        if path == '/static/mono.woff2':
            return self.send_file(os.path.join(FONTS, 'DejaVuSansMono.woff2'), cache=True)
        if path == '/login':
            return self.login_page()
        if path == '/logout':
            key, s = self.session()
            with lock:
                if sessions.pop(key, None) is not None:
                    save_sessions()
            if s:
                log('logout %s dari %s' % (s[0], self.client_address[0]))
            return self.redirect('/login', {'Set-Cookie': 'tr_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax'})
        tok, s = self.session()
        if not s:
            if path.startswith('/api/'):
                return self.json({'error': 'login'}, 401)
            return self.redirect('/login')
        user, role = s[0], s[1]
        if path == '/':
            page = open(os.path.join(WEB, 'index.html'), encoding='utf-8').read()
            boot = json.dumps({'user': user, 'role': role, 'server': server_name()})
            return self.send(200, page.replace('/*__BOOT__*/', 'window.TR=%s;' % boot))
        if path == '/api/state':
            return self.json(state_for(user, role))
        if path == '/api/jobs':
            scope = (qs.get('scope') or ['mine'])[0]
            with lock:
                lst = sorted(jobs.values(), key=lambda x: x['created'], reverse=True)
                lst = [job_public(x) for x in lst if scope == 'all' or x['user'] == user][:300]
            return self.json({'jobs': lst})
        m = re.fullmatch(r'/api/jobs/([a-f0-9]{12})', path)
        if m:
            with lock:
                j = jobs.get(m.group(1))
                if not j:
                    return self.json({'error': 'not found'}, 404)
                d = job_public(j, full=True)
                q = sorted((x for x in jobs.values() if x['status'] == 'queued'), key=lambda x: x['created'])
                d['queue_pos'] = next((i + 1 for i, x in enumerate(q) if x['id'] == j['id']), None)
                d['can_cancel'] = (j['user'] == user or role == 'admin') and j['status'] in ('queued', 'running')
            return self.json(d)
        m = re.fullmatch(r'/thumbs/([a-f0-9]{12})/([^/]+)', path)
        if m:
            return self.send_file(os.path.join(JOBS_DIR, m.group(1) + '_thumbs', os.path.basename(m.group(2))))
        m = re.fullmatch(r'/files/([a-f0-9]{12})/(.+)', path)
        if m:
            with lock:
                j = jobs.get(m.group(1))
            if not j:
                return self.send(404, 'Not found')
            rel = urllib.parse.unquote(m.group(2))
            if rel == '_semua.zip':
                return self.send_zip(j)
            full = os.path.normpath(os.path.join(j['out_dir'], rel))
            if not full.startswith(os.path.normpath(j['out_dir']) + os.sep):
                return self.send(403, 'Forbidden')
            dl = 'dl' in qs
            return self.send_file(full, download_name=os.path.basename(full) if dl else None)
        self.send(404, 'Not found')

    def send_zip(self, j):
        name = os.path.basename(j['out_dir']) + '.zip'
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % urllib.parse.quote(name))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        class W:
            def __init__(s, f):
                s.f, s.n = f, 0

            def write(s, b):
                s.f.write(b)
                s.n += len(b)
                return len(b)

            def tell(s):
                return s.n

            def flush(s):
                s.f.flush()

        with zipfile.ZipFile(W(self.wfile), 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
            for root, _, files in os.walk(j['out_dir']):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, j['out_dir']))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/login':
            return self.do_login()
        if not self.same_origin():
            return self.json({'error': 'origin'}, 403)
        tok, s = self.session()
        if not s:
            return self.json({'error': 'login'}, 401)
        user, role = s[0], s[1]
        if path == '/api/analyze':
            return self.api_analyze(user)
        if path == '/api/jobs':
            return self.api_create(user)
        m = re.fullmatch(r'/api/jobs/([a-f0-9]{12})/cancel', path)
        if m:
            return self.api_cancel(m.group(1), user, role)
        m = re.fullmatch(r'/api/uploads/([a-f0-9]{12})/discard', path)
        if m:
            with lock:
                up = uploads.pop(m.group(1), None)
            if up and up['user'] == user:
                shutil.rmtree(up['dir'], ignore_errors=True)
            return self.json({'ok': True})
        self.json({'error': 'not found'}, 404)

    def do_PUT(self):
        if urllib.parse.urlparse(self.path).path != '/api/upload':
            return self.json({'error': 'not found'}, 404)
        if not self.same_origin():
            return self.json({'error': 'origin'}, 403)
        tok, s = self.session()
        if not s:
            return self.json({'error': 'login'}, 401)
        return self.api_upload(s[0])

    # ---------- handlers
    def do_login(self):
        ip = self.client_address[0]
        with lock:
            n, t0 = fails.get(ip, (0, time.time()))
            if time.time() - t0 >= LOCK_SECS:
                fails.pop(ip, None)
                n = 0
            if n >= MAX_FAILS:
                return self.login_page('Too many attempts. Try again in 5 minutes.', code=429)
        length = min(int(self.headers.get('Content-Length', 0) or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
        user = (form.get('username', [''])[0]).strip().lower()
        pw = form.get('password', [''])[0]
        info = users.load()['users'].get(user)
        if info and users.verify_password(pw, info['password']):
            tok = secrets.token_urlsafe(32)
            with lock:
                sessions[tok_key(tok)] = {'user': user, 'created': time.time(), 'last': time.time(), 'ip': ip}
                fails.pop(ip, None)
                save_sessions()
            log('login OK %s dari %s' % (user, ip))
            return self.redirect('/', {'Set-Cookie': 'tr_session=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax'
                                       % (tok, COOKIE_MAX_AGE)})
        with lock:
            n, t0 = fails.get(ip, (0, time.time()))
            fails[ip] = (n + 1, t0)
        log('login GAGAL %r dari %s' % (user, ip))
        self.login_page('Wrong username or password.', user, 401)

    def api_upload(self, user):
        name = urllib.parse.unquote(self.headers.get('X-Filename', 'file.blend'))
        name = safe_name(os.path.basename(name), 'file.blend')
        ext = os.path.splitext(name)[1].lower()
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        if ext not in ('.blend', '.zip'):
            return self.json({'error': 'Only .blend or .zip files'}, 400)
        if size <= 0 or size > MAX_UPLOAD:
            return self.json({'error': 'Maximum file size is 5 GB'}, 413)
        if shutil.disk_usage('D:\\').free < size * 3 + 20 * 1024 ** 3:
            return self.json({'error': 'Server storage is almost full. Contact IT.'}, 507)
        uid = uuid.uuid4().hex[:12]
        d = os.path.join(UPLOADS, uid)
        os.makedirs(d)
        dst = os.path.join(d, name)
        left = size
        try:
            with open(dst, 'wb') as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 4 * 1024 * 1024))
                    if not chunk:
                        raise IOError('upload terputus')
                    f.write(chunk)
                    left -= len(chunk)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            self.close_connection = True
            return
        with lock:
            uploads[uid] = {'id': uid, 'user': user, 'dir': d, 'file': dst, 'name': name, 'size': size,
                            'time': time.time()}
        log('upload %s %s (%.1f MB) oleh %s' % (uid, name, size / 1e6, user))
        self.json({'id': uid, 'name': name, 'size': size})

    def api_analyze(self, user):
        body = self.read_json()
        with lock:
            up = uploads.get(body.get('id', ''))
        if not up or up['user'] != user:
            return self.json({'error': 'Upload not found, please upload again.'}, 404)
        try:
            src = os.path.join(up['dir'], 'src')
            if up['name'].lower().endswith('.zip'):
                if not os.path.isdir(src):
                    with zipfile.ZipFile(up['file']) as z:
                        base = os.path.realpath(src)
                        for m in z.infolist():
                            target = os.path.realpath(os.path.join(src, m.filename))
                            if not target.startswith(base + os.sep) and target != base:
                                raise RuntimeError('Unsafe zip contents.')
                        z.extractall(src)
                blends = find_blends(src)
                if not blends:
                    raise RuntimeError('No .blend file inside the zip.')
                blend = blends[0]
            else:
                os.makedirs(src, exist_ok=True)
                blend = os.path.join(src, up['name'])
                if not os.path.exists(blend):
                    os.replace(up['file'], blend)
            info = analyse(blend, src)
        except Exception as e:
            return self.json({'error': str(e)}, 422)
        with lock:
            up['blend'] = blend
            up['info'] = info
            qn = sum(1 for x in jobs.values() if x['status'] in ('queued', 'running'))
        info['upload'] = {'id': up['id'], 'name': up['name'], 'size': up['size']}
        info['queue_ahead'] = qn
        self.json(info)

    def api_create(self, user):
        b = self.read_json()
        with lock:
            up = uploads.get(b.get('upload_id', ''))
        if not up or up['user'] != user or 'info' not in up:
            return self.json({'error': 'Upload not found, please upload again.'}, 404)
        info = up['info']
        valid = {c['name'] for c in info['cameras']}
        cams = [c for c in b.get('cameras', []) if c in valid]
        if not cams:
            return self.json({'error': 'Select at least one camera.'}, 400)
        mode = 'anim' if b.get('mode') == 'anim' else 'image'
        res = b.get('res') if b.get('res') in ('file', 'fhd', '2k', '4k', '8k') else 'file'
        samples = b.get('samples')
        samples = int(samples) if isinstance(samples, (int, str)) and str(samples).isdigit() and 1 <= int(samples) <= 65536 else None
        fmt = b.get('format') if b.get('format') in ('file', 'PNG', 'JPEG') else 'file'
        fs, fe = int(b.get('frame_start', info['frame_start'])), int(b.get('frame_end', info['frame_end']))
        if mode == 'anim' and (fe < fs or fe - fs > 20000):
            return self.json({'error': 'Invalid frame range.'}, 400)
        jid = uuid.uuid4().hex[:12]
        title = os.path.splitext(os.path.basename(up['blend']))[0]
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M')
        out = os.path.join(OUT_ROOT, user, '%s_%s' % (safe_name(title), stamp))
        n = 2
        while os.path.exists(out):
            out = os.path.join(OUT_ROOT, user, '%s_%s-%d' % (safe_name(title), stamp, n))
            n += 1
        cam_files, used = {}, set()
        for c in cams:
            base, k = safe_name(c, 'camera'), 2
            nm = base
            while nm.lower() in used:
                nm, k = '%s_%d' % (base, k), k + 1
            used.add(nm.lower())
            cam_files[c] = nm
        # kamera yang terikat marker timeline dirender di frame marker-nya
        # (mis. CAM11_Drawer_Open di frame 11 saat laci terbuka)
        mk = {c['name']: c.get('marker_frame') for c in info['cameras']}
        cam_frames = {c: (mk.get(c) or info['frame_current']) for c in cams}
        jdir = os.path.join(JOBS_DIR, jid + '_src')
        shutil.move(os.path.join(up['dir'], 'src'), jdir)
        blend = os.path.join(jdir, os.path.relpath(up['blend'], os.path.join(up['dir'], 'src')))
        shutil.rmtree(up['dir'], ignore_errors=True)
        j = {'id': jid, 'user': user, 'title': title, 'src_name': up['name'], 'created': now_iso(),
             'status': 'queued', 'mode': mode, 'cameras': cams, 'cam_files': cam_files, 'res': res,
             'samples': samples, 'format': fmt, 'frame': info['frame_current'], 'frame_start': fs,
             'frame_end': fe, 'fps': info['fps'], 'cam_frames': cam_frames, 'video': bool(b.get('video', True)), 'blend': blend,
             'out_dir': out, 'results': [], 'log': [], 'engine': info['engine'],
             'out_w': None, 'out_h': None, 'samples_eff': samples or info['samples']}
        j['progress'] = new_progress(j)
        with lock:
            uploads.pop(up['id'], None)
            jobs[jid] = j
            save_job(j)
        wake.set()
        log('new job %s %s by %s (%s, %d cameras)' % (jid, title, user, mode, len(cams)))
        self.json({'id': jid})

    def api_cancel(self, jid, user, role):
        with lock:
            j = jobs.get(jid)
            if not j:
                return self.json({'error': 'not found'}, 404)
            if j['user'] != user and role != 'admin':
                return self.json({'error': 'Only the submitter or an admin can cancel.'}, 403)
            if j['status'] == 'queued':
                j['status'] = 'canceled'
                j['finished'] = now_iso()
                save_job(j)
                return self.json({'ok': True})
            if j['status'] != 'running':
                return self.json({'error': 'Job already finished.'}, 400)
            j['status'] = 'canceling'
            proc = procs.get(jid)
        if proc:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], creationflags=NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log('job %s dibatalkan oleh %s' % (jid, user))
        self.json({'ok': True})


def main():
    load_jobs()
    load_sessions()
    log('%d sesi login dipulihkan' % len(sessions))
    for fn in (stats_loop, worker_loop, cleanup_loop):
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    srv.daemon_threads = True
    log('Tala Render jalan di port %d' % PORT)
    srv.serve_forever()


if __name__ == '__main__':
    main()
