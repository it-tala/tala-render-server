# Dijalankan: blender -b --factory-startup --disable-autoexec FILE --python inspect_blend.py
# Mencetak satu baris "@@INFO@@{json}" berisi info file untuk form web.
import json
import os

import bpy

s = bpy.context.scene
r = s.render
img = r.image_settings

cams = []
marker_frame = {}
for m in sorted(s.timeline_markers, key=lambda m: m.frame):
    if m.camera is not None and m.camera.name not in marker_frame:
        marker_frame[m.camera.name] = m.frame
for ob in sorted(s.objects, key=lambda o: o.name.lower()):
    if ob.type != 'CAMERA':
        continue
    cams.append({
        'name': ob.name,
        'active': s.camera is not None and ob.name == s.camera.name,
        'hide_render': bool(ob.hide_render),
        'lens': round(ob.data.lens, 1) if ob.data.type == 'PERSP' else None,
        'type': ob.data.type,
        'marker_frame': marker_frame.get(ob.name),
    })

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from relink import relink  # noqa: E402

relinked, missing = relink(os.environ.get('TR_SEARCH'))

engine = r.engine
if engine == 'CYCLES':
    samples = s.cycles.samples
elif engine.startswith('BLENDER_EEVEE'):
    samples = getattr(s.eevee, 'taa_render_samples', None)
else:
    samples = None

info = {
    'scene': s.name,
    'scenes': [sc.name for sc in bpy.data.scenes],
    'engine': engine,
    'samples': samples,
    'res_x': r.resolution_x,
    'res_y': r.resolution_y,
    'res_pct': r.resolution_percentage,
    'out_x': round(r.resolution_x * r.resolution_percentage / 100),
    'out_y': round(r.resolution_y * r.resolution_percentage / 100),
    'frame_start': s.frame_start,
    'frame_end': s.frame_end,
    'frame_current': s.frame_current,
    'fps': round(r.fps / r.fps_base, 3),
    'format': img.file_format,
    'cameras': cams,
    'missing': sorted(set(missing))[:50],
    'relinked': len(relinked),
    'n_images': len(bpy.data.images),
    'packed': sum(1 for im in bpy.data.images if im.packed_file),
    'version': bpy.app.version_string,
    'file_version': '.'.join(str(v) for v in bpy.data.version),
}
print('@@INFO@@' + json.dumps(info), flush=True)
