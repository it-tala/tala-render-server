# Relink file eksternal yang hilang (tekstur, HDRI, library, font, cache) dengan mencari
# nama file yang sama di dalam folder upload (isi .zip). Dipakai oleh inspect & render.
import os

import bpy


def _index(root):
    idx = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            idx.setdefault(f.lower(), []).append(os.path.join(dirpath, f))
    return idx


def _missing(path, lib=None):
    if not path:
        return False
    try:
        p = bpy.path.abspath(path, library=lib)
    except Exception:
        p = path
    return not os.path.exists(p)


def _base(path):
    return os.path.basename(path.replace('\\', '/'))


def external_items():
    """(kind, id, path-attribute) untuk semua data yang menunjuk ke file luar."""
    for im in bpy.data.images:
        if im.source in {'FILE', 'SEQUENCE', 'TILED', 'MOVIE'} and not im.packed_file and im.filepath:
            yield 'image', im, 'filepath'
    for lib in bpy.data.libraries:
        if not lib.packed_file:
            yield 'library', lib, 'filepath'
    for f in bpy.data.fonts:
        if f.filepath and f.filepath != '<builtin>' and not f.packed_file:
            yield 'font', f, 'filepath'
    for v in getattr(bpy.data, 'volumes', []):
        if not v.packed_file and v.filepath:
            yield 'volume', v, 'filepath'
    for c in bpy.data.cache_files:
        yield 'cache', c, 'filepath'
    for s in bpy.data.sounds:
        if not s.packed_file and s.filepath:
            yield 'sound', s, 'filepath'
    for m in bpy.data.movieclips:
        yield 'movie', m, 'filepath'


def relink(root):
    """Cari file yang hilang di dalam `root`. Kembalikan (relinked, still_missing)."""
    relinked, missing = [], []
    if not root or not os.path.isdir(root):
        root = None
    idx = _index(root) if root else {}
    libs_changed = []
    for kind, idb, attr in list(external_items()):
        path = getattr(idb, attr)
        lib = getattr(idb, 'library', None)
        if not _missing(path, lib):
            continue
        name = _base(path)
        hits = idx.get(name.lower(), [])
        if kind == 'image' and idb.source in {'SEQUENCE', 'TILED'} and not hits:
            hits = []
        if hits:
            # pilih kandidat yang paling mirip foldernya
            parts = [p.lower() for p in path.replace('\\', '/').split('/')[:-1]]
            hits.sort(key=lambda h: -sum(1 for p in parts if p and p in h.lower().replace('\\', '/').split('/')))
            try:
                setattr(idb, attr, hits[0])
                if kind == 'library':
                    libs_changed.append(idb)
                elif kind == 'image':
                    idb.reload()
                relinked.append(name)
                continue
            except Exception:
                pass
        missing.append(name or idb.name)
    for lib in libs_changed:
        try:
            lib.reload()
        except Exception:
            pass
    return sorted(set(relinked)), sorted(set(missing))
