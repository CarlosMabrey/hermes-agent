"""Private, immutable attachment bundles for one admitted prompt."""
from dataclasses import dataclass
from pathlib import Path
import base64
import io
import os
import uuid
import hashlib
import threading
from PIL import Image, ImageOps
from .explicit_attachment_storage import OwnedFile, protected_directory, create_file

PROMPT_EXPLICIT_ATTACHMENTS_V1 = True

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_STAGED_BYTES = 256 * 1024 * 1024
_stage_lock = threading.Lock()

MAX_PIXELS = 40_000_000
MIME_FORMATS = {'image/png': ('PNG', '.png'), 'image/jpeg': ('JPEG', '.jpg'), 'image/webp': ('WEBP', '.webp')}

@dataclass(frozen=True)
class AttachmentBundle:
    paths: tuple[str, ...] = ()
    identities: tuple[tuple[int, int, int, int, str], ...] = ()

    owners: tuple = ()

    def cleanup(self):
        """Deletion authority is an open object, never a checked pathname."""
        try:
            for name, identity, owner in zip(self.paths, self.identities, self.owners):
                try:
                    info = Path(name).stat(follow_symlinks=False)
                    metadata = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    unchanged = metadata == identity[:4] and _file_identity(Path(name)) == identity
                except (OSError, ValueError):
                    unchanged = False
                if unchanged:
                    owner.cleanup()
        finally:
            for owner in self.owners:
                owner.close()



def _file_identity(path):
    info = path.stat(follow_symlinks=False)
    metadata = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if info.st_size > MAX_IMAGE_BYTES:
        raise ValueError("attachment exceeds digest read limit")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != metadata:
            raise ValueError("attachment changed before digest read")
        raw = stream.read(MAX_IMAGE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) != info.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != metadata:
        raise ValueError("attachment changed during digest read")
    return (*metadata, hashlib.sha256(raw).hexdigest())

def stage(session, attachments):
    if len(attachments) > MAX_IMAGES:
        raise ValueError("too many images")
    decoded = []
    total = 0
    output_total = 0
    for item in attachments:
        name = item.get('filename') or ''
        if any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name):
            raise ValueError("filename must be a display label")
        expected, ext = MIME_FORMATS[item['mime_type']]
        suffix = Path(name).suffix.lower()
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
        if name and (name.endswith((' ', '.')) or name.split('.')[0].upper() in reserved
                     or (suffix and suffix not in ({'.jpg', '.jpeg'} if expected == 'JPEG' else {ext}))):
            raise ValueError("unsafe filename or image extension disagreement")
        encoded = item['content_base64']
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("encoded image too large")
        try:
            raw = base64.b64decode(encoded, validate=True)
            total += len(raw)
            if not raw or len(raw) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("image byte limit")
            expected, ext = MIME_FORMATS[item['mime_type']]
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != expected or image.width * image.height > MAX_PIXELS:
                    raise ValueError("image format or pixel limit")
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError("animated images are not supported")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()  # verify() alone does not decode JPEG pixel data
                oriented = ImageOps.exif_transpose(image)
                pixels = oriented.convert('RGBA' if 'A' in oriented.getbands() else 'RGB')
                clean = Image.new(pixels.mode, pixels.size)
                clean.paste(pixels)
                output = io.BytesIO()
                clean.save(output, format='PNG')
                raw = output.getvalue()
                output_total += len(raw)
                if len(raw) > MAX_IMAGE_BYTES or output_total > MAX_TOTAL_BYTES:
                    raise ValueError("normalized image byte limit")
                ext = '.png'
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("invalid image") from exc
        decoded.append((raw, ext))
    if not decoded:
        return AttachmentBundle()
    root = Path(session['profile_home']) / 'images' / 'explicit-turns'
    paths, identities, owners = [], [], []
    try:
        with _stage_lock, protected_directory(root) as directory_fd:
            with os.scandir(root if directory_fd is None else directory_fd) as entries:
                retained = sum(entry.stat(follow_symlinks=False).st_size for entry in entries)
            if retained + output_total > MAX_STAGED_BYTES:
                raise ValueError("explicit attachment retention budget exhausted")
            for raw, ext in decoded:
                path = root / (uuid.uuid4().hex + ext)
                owner = None
                try:
                    with create_file(path, directory_fd) as stream:
                        owner = OwnedFile(stream)
                        stream.write(raw)
                        stream_info = os.fstat(stream.fileno())
                        created = (stream_info.st_dev, stream_info.st_ino)
                    identity = _file_identity(path)
                    if identity[:2] != created or identity[2] != len(raw) or identity[4] != hashlib.sha256(raw).hexdigest():
                        raise OSError("staged attachment path was replaced")
                except Exception:
                    if owner is not None:
                        owner.cleanup()
                    raise
                paths.append(str(path))
                identities.append(identity)
                owners.append(owner)
        return AttachmentBundle(tuple(paths), tuple(identities), tuple(owners))
    except Exception:
        AttachmentBundle(tuple(paths), tuple(identities), tuple(owners)).cleanup()
        raise
