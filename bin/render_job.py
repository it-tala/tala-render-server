# Dipakai server web. Urutan argumen Blender (dibuat oleh server):
#   blender -b --disable-autoexec --python force_gpu.py FILE --python render_job.py
#           --python-expr "...cam_start(0)" -f N --python-expr "...cam_done(0)"   (mode gambar, per kamera)
#           --python-expr "...cam_start(0)" -s A -e B -a --python-expr "...cam_done(0)"  (mode animasi)
#           --python-expr "...end()"
# Job dibaca dari env TR_JOB. Render lewat argumen CLI supaya Blender mencetak progres sample.
import json
import os

import bpy

LONG_SIDE = {'fhd': 1920, '2k': 2560, '4k': 3840, '8k': 7680}
VIDEO = {'FFMPEG', 'AVI_JPEG', 'AVI_RAW'}


def _job():
    with open(os.environ['TR_JOB'], encoding='utf-8') as f:
        return json.load(f)


def mark(tag, **kw):
    print('@@%s@@%s' % (tag, json.dumps(kw)), flush=True)


def _image_format(img, fmt):
    try:
        img.media_type = 'IMAGE'
    except Exception:
        pass
    img.file_format = fmt


def setup():
    job = _job()
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from relink import relink
    relinked, missing = relink(os.environ.get('TR_SEARCH'))
    mark('RELINK', relinked=len(relinked), missing=missing[:50])
    s = bpy.context.scene
    # Marker timeline yang terikat kamera akan menimpa scene.camera saat render.
    # Server memilih kamera sendiri per angle, jadi ikatan marker dilepas (hanya di memori).
    for m in s.timeline_markers:
        if m.camera is not None:
            m.camera = None
    r = s.render
    img = r.image_settings
    # resolusi (perbandingan sisi tetap mengikuti file)
    w = r.resolution_x * r.resolution_percentage / 100
    h = r.resolution_y * r.resolution_percentage / 100
    target = LONG_SIDE.get(job['res'])
    if target:
        k = target / max(w, h)
        w, h = w * k, h * k
    r.resolution_x, r.resolution_y = max(4, int(round(w / 2) * 2)), max(4, int(round(h / 2) * 2))
    r.resolution_percentage = 100
    # kualitas
    if job.get('samples'):
        if r.engine == 'CYCLES':
            s.cycles.samples = int(job['samples'])
            s.cycles.use_denoising = True
        elif hasattr(s.eevee, 'taa_render_samples'):
            s.eevee.taa_render_samples = int(job['samples'])
    # format
    fmt = job.get('format') or 'file'
    if fmt in ('PNG', 'JPEG'):
        _image_format(img, fmt)
        if fmt == 'JPEG':
            img.quality = 92
    elif img.file_format in VIDEO:
        _image_format(img, 'PNG')
    r.use_file_extension = True
    r.use_overwrite = True
    r.use_placeholder = False
    if job['mode'] == 'anim':
        # Simpan BVH/tekstur di GPU antar frame -> frame ke-2 dst. tidak memuat ulang scene dari nol
        r.use_persistent_data = True
    samples = s.cycles.samples if r.engine == 'CYCLES' else getattr(s.eevee, 'taa_render_samples', None)
    mark('START', w=r.resolution_x, h=r.resolution_y, engine=r.engine, samples=samples, ext=r.file_extension)


def cam_start(i):
    job = _job()
    s = bpy.context.scene
    r = s.render
    name = job['cameras'][i]
    cam = s.objects.get(name)
    if cam is not None and cam.type == 'CAMERA':
        s.camera = cam
    safe = job['cam_files'][name]
    if job['mode'] == 'image':
        r.filepath = os.path.join(job['out_dir'], safe + '_tmp')
    else:
        fdir = os.path.join(job['out_dir'], safe, 'frames')
        os.makedirs(fdir, exist_ok=True)
        r.filepath = os.path.join(fdir, safe + '_')   # -> <nama>_0001.png
    mark('CAM_START', name=name, i=i)


def cam_done(i):
    job = _job()
    r = bpy.context.scene.render
    name = job['cameras'][i]
    safe = job['cam_files'][name]
    if job['mode'] == 'image':
        frame = job.get('cam_frames', {}).get(name, job['frame'])
        src = os.path.join(job['out_dir'], '%s_tmp%04d%s' % (safe, frame, r.file_extension))
        dst = os.path.join(job['out_dir'], safe + r.file_extension)
        if os.path.exists(src):
            os.replace(src, dst)
        mark('CAM_DONE', name=name, i=i, file=os.path.basename(dst), ok=os.path.exists(dst))
    else:
        mark('CAM_DONE', name=name, i=i, file=safe, ok=True)


def end():
    mark('END')


if __name__ == '__main__':
    setup()
