"""Strips embedded artwork from FLAC files and copies source art (an
`Artwork`/`scans`/etc. folder, or failing that a lone loose image file
alongside the tracks) into the destination album directory, renamed to
the "<category>-NN" convention (matching the Cover Art Archive type
vocabulary: front, back, booklet, medium, tray, spine, liner, sticker,
poster, obi, watermark, other) that Roon reads directly from the folder.
A single unlabeled loose image (cover.jpg, folder.jpg, or any other
name) is treated as the front cover, per near-universal ripping
convention. Supplements that folder with
images from the matched release's MusicBrainz Cover Art Archive listing:
for a category with zero local coverage, CAA becomes the canonical source
and gets the plain "<category>-NN" name (so e.g. front-01 reliably exists
even when local filenames give no usable signal -- generic "image_NN.jpg"
scans, etc. -- with no manual review needed); for a category that already
has local images, CAA only supplements it and gets "CAA-<category>-NN"
with its own independent numbering, so it's never confused with
locally-sourced art. Exact byte-size duplicates within the same category
are skipped. Also writes a VERSION tag ("{media} | {edition}") and a
$formatcode path template field.

List this plugin AFTER `scrub` in the plugins config: scrub's automatic
restore step re-embeds any art that was present in the source file, so
stripping needs to run after that, not before.
"""

import os
import shutil

import mediafile
import mutagen
import requests
from mutagen.flac import FLAC

from beets.plugins import BeetsPlugin
from beets.util import bytestring_path, displayable_path, syspath

IMAGE_EXTENSIONS = {b'jpg', b'jpeg', b'png', b'webp'}

# Source subdirectory names (case-insensitive) recognized as holding
# artwork to process, beyond the canonical "Artwork".
ARTWORK_DIR_NAMES = {'artwork', 'scans', 'scan', 'covers', 'cover', 'images'}

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


def disc_prefix(item):
    # "02-" for track 1 of a 2+ disc release; "" for single-disc albums.
    # beets' path templates have no native numeric comparison, so this is
    # computed here rather than with %if{}.
    if (item.disctotal or 1) > 1:
        return '%02d-' % item.disc
    return ''


def compose_version(media, disambig):
    # "{media} | {albumdisambig}", but collapsed to just one side when
    # they're the same value (e.g. media=SACD, albumdisambig=SACD would
    # otherwise write "SACD | SACD") -- same rationale as
    # distinct_disambig() below, applied to the VERSION tag instead of
    # the path.
    media = (media or '').strip()
    disambig = (disambig or '').strip()
    if media and disambig and media.lower() == disambig.lower():
        return media
    return ' | '.join(p for p in (media, disambig) if p)


def smart_title(text):
    # Like beets' own %title{} (string.capwords), but preserves words
    # that are already all-caps in the source (e.g. "US", "UK", "SACD")
    # instead of lowercasing them to "Us"/"Uk"/"Sacd". origin.yaml Edition
    # text is free-form, so acronyms like "US Pressing" show up often.
    words = []
    for word in text.split(' '):
        if word.isupper() and len(word) > 1:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return ' '.join(words)


def distinct_disambig(item):
    # albumdisambig, but suppressed when it's redundant with the [media]
    # path segment (e.g. both "SACD") -- beets' path templates have no
    # native string-equality comparison, so this is computed here rather
    # than with %if{}. Also applies smart_title() here (see above) and
    # returns it pre-formatted -- the path template uses $distinctdisambig
    # directly, without wrapping it in %title{}, since that would undo
    # the acronym preservation.
    disambig = (item.albumdisambig or '').strip()
    media = (item.media or '').strip()
    # Match the same "before the first ' ('" truncation the path template
    # applies to $media (%first{$media,1,0, (}), so this compares against
    # what actually appears in the folder name's [] segment.
    media_shown = media.split(' (', 1)[0].strip()
    if disambig and media_shown and disambig.lower() == media_shown.lower():
        return ''
    return smart_title(disambig) if disambig else disambig

# Keyword -> canonical category, checked in this order (most specific/
# aliases first) against the lowercased filename. Anything unmatched
# falls back to "other".
KEYWORD_CATEGORIES = [
    ('booklet', 'booklet'),
    ('inside', 'booklet'),
    ('page', 'booklet'),
    ('front', 'front'),
    ('cover', 'front'),
    ('folder', 'front'),
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
        # beet move/modify -m relocates only the files beets actually
        # tracks as library items -- a plain move leaves the Artwork/
        # folder behind in the old directory, since it was only ever
        # built at import time (build_artwork_folder below), not
        # tracked. Carry it along whenever an already-imported album
        # gets moved later.
        self.register_listener('item_moved', self.item_moved)
        self.template_fields['formatcode'] = format_code
        self.template_fields['discprefix'] = disc_prefix
        self.template_fields['distinctdisambig'] = distinct_disambig
        self.add_media_field('version', mediafile.MediaField(
            mediafile.MP3DescStorageStyle(desc='VERSION'),
            mediafile.StorageStyle('VERSION'),
        ))
        self.config.add({'fetch_caa_art': True})

    def handle_task(self, task, session):
        self.strip_embedded_art(task)
        self.build_artwork_folder(task)

    def item_moved(self, item, source, destination):
        source_dir = os.path.dirname(source)
        dest_dir = os.path.dirname(destination)
        if source_dir == dest_dir:
            return
        old_artwork = os.path.join(source_dir, b'Artwork')
        new_artwork = os.path.join(dest_dir, b'Artwork')
        if not os.path.isdir(syspath(old_artwork)):
            return
        if os.path.exists(syspath(new_artwork)):
            return  # already carried along by an earlier item in this album
        try:
            shutil.move(syspath(old_artwork), syspath(new_artwork))
        except OSError as exc:
            self._log.warning('Could not carry Artwork folder to new location: {0}'.format(exc))

    def write_version_tag(self, item, path, tags):
        # originquery prompts (when running interactively, and only when
        # MB and origin data genuinely disagree) for which source should
        # win for VERSION specifically; respect that if present.
        version = item.get('version_choice')
        if not version:
            # media/albumdisambig already resolve origin data over
            # MusicBrainz's own (via originquery), falling back to
            # MusicBrainz alone when no origin file exists -- reuse that
            # resolved state as-is.
            version = compose_version(item.get('media'), item.get('albumdisambig'))
            if not version:
                return
        item['version'] = version
        tags['version'] = version

    def strip_embedded_art(self, task):
        for item in task.items:
            path = syspath(item.path)
            try:
                f = mutagen.File(path)
            except Exception as exc:
                self._log.warning(
                    'roon_artwork: could not open {} to strip art: {}',
                    displayable_path(item.path), exc,
                )
                continue
            if f is None:
                continue

            if isinstance(f, FLAC):
                if not f.pictures:
                    continue
                f.clear_pictures()
            elif f.tags is not None and hasattr(f.tags, 'getall'):
                # ID3-based tags (DSF, MP3, ...): pictures are APIC frames.
                if not f.tags.getall('APIC'):
                    continue
                f.tags.delall('APIC')
            else:
                continue

            f.save()
            self._log.info(
                'roon_artwork: stripped embedded art from {}',
                displayable_path(item.path),
            )

    def _candidate_dirs(self, task):
        """Directories to search for source art: the common parent of
        task.paths first (for multi-disc albums, task.paths is each disc's
        own directory -- e.g. "Disc 1"/"Disc 2" -- so a shared Artwork/
        scans/ folder sitting at the album's top level, a sibling of both
        disc folders, would otherwise never be found), then each
        individual path.
        """
        if not task.paths:
            return []
        dirs = []
        if len(task.paths) > 1:
            try:
                common = os.path.commonpath(task.paths)
                if common:
                    dirs.append(common)
            except ValueError:
                pass
        for path in task.paths:
            if path not in dirs:
                dirs.append(path)
        return dirs

    def _find_source_artwork_dir(self, task):
        for path in self._candidate_dirs(task):
            if not os.path.isdir(syspath(path)):
                continue
            for entry in os.listdir(syspath(path)):
                if entry.lower() in ARTWORK_DIR_NAMES:
                    candidate = os.path.join(path, bytestring_path(entry))
                    if os.path.isdir(syspath(candidate)):
                        return candidate
        return None

    def _find_loose_root_images(self, task):
        """Image files directly in the album's source directory, for
        albums with no Artwork/scans/etc. subfolder at all -- just a lone
        cover.jpg/folder.jpg/whatever alongside the tracks. Returns
        (source_dir, [filenames]) or (None, [])."""
        for path in self._candidate_dirs(task):
            if not os.path.isdir(syspath(path)):
                continue
            images = []
            for entry in sorted(os.listdir(syspath(path))):
                fn = bytestring_path(entry)
                if b'.' not in fn:
                    continue
                ext = fn.rsplit(b'.', 1)[-1].lower()
                full = os.path.join(path, fn)
                if ext in IMAGE_EXTENSIONS and os.path.isfile(syspath(full)):
                    images.append(fn)
            if images:
                return path, images
        return None, []

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
        loose_root = source_artwork is None
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
            source_dir = source_artwork
        else:
            # No Artwork/scans/etc. subfolder -- fall back to whatever
            # loose image file(s) sit alongside the tracks (cover.jpg,
            # folder.jpg, or a fully custom name).
            source_dir, images = self._find_loose_root_images(task)

        if images:
            categorized = [
                (fn, categorize(fn.decode('utf-8', 'replace'))) for fn in images
            ]
            if loose_root and all(cat == 'other' for _, cat in categorized):
                # A single unlabeled image alongside the tracks is, by
                # near-universal ripping convention, the front cover.
                fn0, _ = categorized[0]
                categorized[0] = (fn0, 'front')

            os.makedirs(syspath(dest_artwork), exist_ok=True)
            for fn, category in categorized:
                counters[category] = counters.get(category, 0) + 1
                ext = fn.rsplit(b'.', 1)[-1].lower()
                new_name = f'{category}-{counters[category]:02d}.'.encode() + ext
                src = os.path.join(source_dir, fn)
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
            self._add_caa_art(task, dest_artwork, counters, sizes_by_category)

    def _add_caa_art(self, task, dest_artwork, counters, sizes_by_category):
        # Categories with zero local coverage: CAA becomes the canonical
        # (unprefixed) source for these, so e.g. front-01 reliably exists
        # even when local filenames give no usable signal (generic
        # "image_NN.jpg" scans, etc.) -- no visual review required. When a
        # category already has local coverage, CAA images only supplement
        # it and get the CAA- prefix to stay distinguishable.
        locally_covered = set(counters)
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

            if category in locally_covered:
                caa_counters[category] = caa_counters.get(category, 0) + 1
                new_name = (
                    f'CAA-{category}-{caa_counters[category]:02d}.'.encode() + ext
                )
            else:
                counters[category] = counters.get(category, 0) + 1
                new_name = f'{category}-{counters[category]:02d}.'.encode() + ext
            dst = os.path.join(dest_artwork, new_name)
            with open(syspath(dst), 'wb') as f:
                f.write(content)
            sizes_by_category.setdefault(category, set()).add(len(content))
            self._log.info(
                'roon_artwork: added CAA image -> {}',
                displayable_path(new_name),
            )
