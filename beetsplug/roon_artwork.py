"""Strips embedded artwork from FLAC files and copies a source `Artwork/`
folder into the destination album directory, renamed to the "<category>-NN"
convention (matching the Cover Art Archive type vocabulary: front, back,
booklet, medium, tray, spine, liner, sticker, poster, obi, watermark, other)
that Roon reads directly from the folder. Supplements that folder with any
images from the matched release's MusicBrainz Cover Art Archive listing
that aren't already covered locally (exact byte-size duplicates within the
same category are skipped); those are named "CAA-<category>-NN" with their
own independent per-category numbering, so they're never confused with
locally-sourced art. If there's no local Artwork/ folder at all, CAA
becomes the sole source instead of being skipped. Also writes a VERSION
tag ("{media} | {edition}") and a $formatcode path template field.

List this plugin AFTER `scrub` in the plugins config: scrub's automatic
restore step re-embeds any art that was present in the source file, so
stripping needs to run after that, not before.
"""

import os
import shutil

import mediafile
import requests
from mutagen.flac import FLAC

from beets.plugins import BeetsPlugin
from beets.util import bytestring_path, displayable_path, syspath

IMAGE_EXTENSIONS = {b'jpg', b'jpeg', b'png', b'webp'}

CAA_URL = 'https://coverartarchive.org/release/{}'
CAA_USER_AGENT = 'beets-roon-artwork/0.1 ( local beets plugin, no contact )'
CAA_CONTENT_TYPE_EXT = {
    'image/jpeg': b'jpg',
    'image/png': b'png',
    'image/webp': b'webp',
}

# Cover Art Archive's own type vocabulary -> our category slugs. Anything
# not listed falls back to a slugified version of the CAA type name itself.
CAA_TYPE_CATEGORIES = {
    'Front': 'front',
    'Back': 'back',
    'Booklet': 'booklet',
    'Medium': 'medium',
    'Tray': 'tray',
    'Spine': 'spine',
    'Liner': 'liner',
    'Sticker': 'sticker',
    'Poster': 'poster',
    'Obi': 'obi',
    'Watermark': 'watermark',
}


def caa_category(types):
    primary = (types or ['Other'])[0]
    return CAA_TYPE_CATEGORIES.get(
        primary, primary.lower().replace('/', '-').replace(' ', '-')
    )

# beets' own $format field spells some formats out in full (e.g. "DSD Stream
# File" for .dsf) rather than the short code useful as a path segment.
# Anything not listed here passes through $format unchanged.
FORMAT_CODES = {
    'DSD Stream File': 'DSF',
}


def format_code(item):
    return FORMAT_CODES.get(item.format, item.format)

# Keyword -> canonical category, checked in this order (most specific/
# aliases first) against the lowercased filename. Anything unmatched
# falls back to "other".
KEYWORD_CATEGORIES = [
    ('booklet', 'booklet'),
    ('inside', 'booklet'),
    ('front', 'front'),
    ('back', 'back'),
    ('medium', 'medium'),
    ('disc', 'medium'),
    ('tray', 'tray'),
    ('spine', 'spine'),
    ('liner', 'liner'),
    ('sticker', 'sticker'),
    ('poster', 'poster'),
    ('obi', 'obi'),
    ('watermark', 'watermark'),
]


def categorize(filename):
    lower = filename.lower()
    for keyword, category in KEYWORD_CATEGORIES:
        if keyword in lower:
            return category
    return 'other'


class RoonArtworkPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.register_listener('import_task_files', self.handle_task)
        self.register_listener('write', self.write_version_tag)
        self.template_fields['formatcode'] = format_code
        self.add_media_field('version', mediafile.MediaField(
            mediafile.StorageStyle('VERSION'),
        ))
        self.config.add({'fetch_caa_art': True})

    def handle_task(self, task, session):
        self.strip_embedded_art(task)
        self.build_artwork_folder(task)

    def write_version_tag(self, item, path, tags):
        # media/albumdisambig already resolve origin data over MusicBrainz's
        # own (via originquery), falling back to MusicBrainz alone when no
        # origin file exists -- reuse that resolved state as-is.
        parts = [p for p in (item.get('media'), item.get('albumdisambig')) if p]
        if not parts:
            return
        version = ' | '.join(parts)
        item['version'] = version
        tags['version'] = version

    def strip_embedded_art(self, task):
        for item in task.items:
            path = syspath(item.path)
            try:
                f = FLAC(path)
            except Exception as exc:
                self._log.warning(
                    'roon_artwork: could not open {} to strip art: {}',
                    displayable_path(item.path), exc,
                )
                continue
            if f.pictures:
                f.clear_pictures()
                f.save()
                self._log.info(
                    'roon_artwork: stripped embedded art from {}',
                    displayable_path(item.path),
                )

    def _find_source_artwork_dir(self, task):
        if not task.paths:
            return None
        for path in task.paths:
            if not os.path.isdir(syspath(path)):
                continue
            for entry in os.listdir(syspath(path)):
                if entry.lower() == 'artwork':
                    candidate = os.path.join(path, bytestring_path(entry))
                    if os.path.isdir(syspath(candidate)):
                        return candidate
        return None

    def build_artwork_folder(self, task):
        album = getattr(task, 'album', None)
        try:
            if album is not None:
                dest_album_dir = album.item_dir()
            else:
                dest_album_dir = os.path.dirname(task.items[0].path)
        except (ValueError, AttributeError, IndexError):
            self._log.warning('roon_artwork: no destination album directory')
            return
        dest_artwork = os.path.join(dest_album_dir, b'Artwork')

        # category -> next sequence number, and category -> byte sizes
        # already placed (cheap exact-duplicate detection for CAA images).
        counters = {}
        sizes_by_category = {}

        source_artwork = self._find_source_artwork_dir(task)
        if source_artwork is not None:
            images = []
            for fn in sorted(os.listdir(syspath(source_artwork))):
                fn = bytestring_path(fn)
                if b'.' not in fn:
                    continue
                ext = fn.rsplit(b'.', 1)[-1].lower()
                full = os.path.join(source_artwork, fn)
                if ext in IMAGE_EXTENSIONS and os.path.isfile(syspath(full)):
                    images.append(fn)

            if images:
                os.makedirs(syspath(dest_artwork), exist_ok=True)
            for fn in images:
                category = categorize(fn.decode('utf-8', 'replace'))
                counters[category] = counters.get(category, 0) + 1
                ext = fn.rsplit(b'.', 1)[-1].lower()
                new_name = f'{category}-{counters[category]:02d}.'.encode() + ext
                src = os.path.join(source_artwork, fn)
                dst = os.path.join(dest_artwork, new_name)
                shutil.copyfile(syspath(src), syspath(dst))
                sizes_by_category.setdefault(category, set()).add(
                    os.path.getsize(syspath(dst))
                )
                self._log.info(
                    'roon_artwork: {} -> {}',
                    displayable_path(fn), displayable_path(new_name),
                )

        if self.config['fetch_caa_art'].get(bool):
            self._add_caa_art(task, dest_artwork, sizes_by_category)

    def _add_caa_art(self, task, dest_artwork, sizes_by_category):
        mbid = task.items[0].get('mb_albumid') if task.items else None
        if not mbid:
            return

        try:
            resp = requests.get(
                CAA_URL.format(mbid),
                headers={'User-Agent': CAA_USER_AGENT},
                timeout=10,
            )
            if resp.status_code == 404:
                return
            resp.raise_for_status()
            caa_images = resp.json().get('images', [])
        except (requests.RequestException, ValueError) as exc:
            self._log.warning(
                'roon_artwork: could not reach Cover Art Archive: {}', exc,
            )
            return

        made_dir = os.path.isdir(syspath(dest_artwork))
        caa_counters = {}
        for img in caa_images:
            category = caa_category(img.get('types'))
            url = img.get('image')
            if not url:
                continue
            try:
                img_resp = requests.get(
                    url, headers={'User-Agent': CAA_USER_AGENT}, timeout=20,
                )
                img_resp.raise_for_status()
            except requests.RequestException as exc:
                self._log.warning(
                    'roon_artwork: could not download {}: {}', url, exc,
                )
                continue

            content = img_resp.content
            if len(content) in sizes_by_category.get(category, ()):
                self._log.debug(
                    'roon_artwork: skipping likely-duplicate CAA {} image',
                    category,
                )
                continue

            ext = CAA_CONTENT_TYPE_EXT.get(
                img_resp.headers.get('Content-Type', '').split(';')[0].strip(),
                b'jpg',
            )
            if not made_dir:
                os.makedirs(syspath(dest_artwork), exist_ok=True)
                made_dir = True

            caa_counters[category] = caa_counters.get(category, 0) + 1
            new_name = f'CAA-{category}-{caa_counters[category]:02d}.'.encode() + ext
            dst = os.path.join(dest_artwork, new_name)
            with open(syspath(dst), 'wb') as f:
                f.write(content)
            sizes_by_category.setdefault(category, set()).add(len(content))
            self._log.info(
                'roon_artwork: added CAA image -> {}',
                displayable_path(new_name),
            )
