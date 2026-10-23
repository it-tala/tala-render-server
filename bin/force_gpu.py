# Dijalankan oleh Flamenco sebelum file .blend dibuka.
# Memaksa Cycles merender pakai GPU (OptiX, fallback CUDA) di RTX 5090.
import bpy
from bpy.app.handlers import persistent


def setup_prefs():
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for backend in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = backend
            prefs.get_devices()
            gpus = [d for d in prefs.devices if d.type == backend]
            if gpus:
                for d in prefs.devices:
                    d.use = (d.type == backend)
                print(f"[force_gpu] {backend}: {[d.name for d in gpus]}")
                return True
        except Exception as e:
            print(f"[force_gpu] {backend} gagal: {e}")
    print("[force_gpu] GPU tidak ditemukan, render pakai CPU")
    return False


GPU_OK = setup_prefs()


@persistent
def _force_gpu(_dummy):
    if not GPU_OK:
        return
    for scene in bpy.data.scenes:
        if scene.render.engine == 'CYCLES':
            scene.cycles.device = 'GPU'
            print(f"[force_gpu] scene '{scene.name}' -> GPU")


bpy.app.handlers.load_post.append(_force_gpu)
