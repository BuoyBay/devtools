#!/usr/bin/env python3
"""Build an offline BuoyDocs snapshot from upstream and installed package data."""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import html
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit
import zipfile
import zlib

DOCS = 'resources/buoy-docs/'
MIB = 1024 * 1024
MAX_ARCHIVE = 256 * MIB
MAX_ENTRY = MIB
MAX_IMAGE = 4 * MIB
MAX_DOCS = 16 * MIB
MAX_OUTPUT = 128 * MIB
MAX_ENTRIES = 16384
PACKAGE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'filesystem symlink forbidden: ' + str(part))
    return path


def global_directory():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/buoy/packages'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'buoy/packages'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'buoy/packages'


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    def reject(value):
        raise ValueError('invalid JSON number: ' + value)
    return json.loads(data, object_pairs_hook=pairs, parse_constant=reject)


def metadata(source):
    """Deliberately small YAML subset, not a permissive YAML approximation."""
    lines = source.splitlines()
    if not lines or lines[0] != '---':
        return {}, source
    require('---' in lines[1:], 'unterminated front matter')
    end = lines.index('---', 1)
    require(end <= 128, 'front matter exceeds 128 lines')
    result = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        match = re.fullmatch(r'([a-z]+):[ ]*(.*)', line)
        require(match is not None, 'unsupported front matter syntax')
        key, raw = match.groups()
        require(key in ('title', 'summary', 'keywords', 'symbols', 'category', 'order', 'types', 'related', 'dependencies'), 'unknown front matter key: ' + key)
        require(key not in result, 'duplicate front matter key: ' + key)
        if key == 'order':
            require(re.fullmatch(r'-?[0-9]{1,6}', raw), 'order must be an integer of at most six digits')
            value = int(raw)
        elif key in ('keywords', 'symbols', 'types', 'related', 'dependencies'):
            value = strict_json(raw)
            require(isinstance(value, list) and len(value) <= 64, key + ' must be a JSON array of strings (max 64)')
            require(all(isinstance(v, str) and 0 < len(v) <= 256 and not any(ord(c) < 32 for c in v) for v in value), 'invalid ' + key)
        else:
            if raw.startswith('"'):
                value = strict_json(raw)
            else:
                require(raw and not any(c in raw for c in ':#[]{}&*!|>\'"') and raw == raw.strip(), 'unsupported plain scalar; use a JSON quoted string')
                value = raw
            require(isinstance(value, str) and 0 < len(value) <= 2048 and not any(ord(c) < 32 for c in value), 'invalid scalar: ' + key)
        if key == 'dependencies':
            require(all(PACKAGE.fullmatch(v) for v in value), 'invalid dependency name')
        if key == 'types':
            require(all(re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', v) for v in value), 'invalid type name')
        result[key] = value
    return result, '\n'.join(lines[end + 1:])


def slug(value):
    return re.sub(r'\s+', '-', re.sub(r'[^\w\s-]', '', value.lower()).strip())


def index_definitions(page):
    """Attach stable member anchors to authored headings and API table rows."""
    reference = page.get('section') == 'reference' or (page.get('section') == 'packages' and re.fullmatch(r'(?:Reference|API)', page.get('category', ''), re.I))
    if not reference:
        page.pop('definitions', None)
        return
    source = page.get('html', '')
    starts = [0]
    starts.extend(m.end() for m in re.finditer('\n', source))
    used = set(re.findall(r'\bid=[\"\']([^\"\']+)', source))
    definitions = [d for d in page.get('definitions', []) if isinstance(d, dict) and d.get('kind') == 'keyword' and d.get('anchor') in used]
    edits = []
    declared = set(page.get('types', []))
    title = page.get('title', '').split(' Of ')[0].split(' (')[0]
    default_owner = {'Strings': 'String', 'Arrays': 'Array'}.get(title, title)
    if not re.fullmatch(r'[A-Za-z_]\w*', default_owner):
        default_owner = ''
    boilerplate = {'properties','methods','events','fields','values','constants','parameters','returns','summary','syntax','example','examples','notes','unused-boilerplate'}
    section_adjectives = {'shared','instance','type','default','public','private','protected','supported','synthesized','extension','inherited','common','available','required','optional','abstract','static'}
    symbol = r'[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*'
    # Embedded reference sections are type declarations too. Match documented
    # heading conventions, rather than indexing every capitalized prose heading.
    type_headings = [
        re.compile(r'^(' + symbol + r') (?:properties|enumerations|enum)(?: \([^)]*\))?$', re.I),
        re.compile(r'^(?:properties|class|structure|interface|enum) \((' + symbol + r')\)$', re.I),
        re.compile(r'^(?:class|structure|interface|(?:flags )?enum):\s*(' + symbol + r')$', re.I),
        re.compile(r'^(' + symbol + r') \((?:(?:flags )?enum|base class(?:\s*[—-].*)?|(?:inherits|extends) .+)\)$', re.I),
    ]

    class DefinitionParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.owner = default_owner
            self.type_owners = {}
            self.heading = None
            self.table = None
            self.row = None
            self.cell = None
            self.type_names = set()
            self.enum_level = None

        def type_anchor(self, element, name):
            # Properties and enumeration sections can document the same type;
            # choose its first section consistently instead of creating ambiguity.
            if name.lower() not in self.type_names:
                self.anchor(element, name, 'type')
                self.type_names.add(name.lower())

        def anchor(self, element, name, kind):
            ident = element['attrs'].get('id')
            if not ident:
                base = 'definition-' + slug(name)
                ident, n = base, 2
                while ident in used:
                    ident, n = base + '-' + str(n), n + 1
                used.add(ident)
                element['attrs']['id'] = ident
                # Offsets point to the opening tag, not a guessed matching string.
                end = source.index('>', element['offset'])
                edits.append((end, ' id="' + html.escape(ident, quote=True) + '"'))
            entry = dict(name=name, anchor=ident, kind=kind)
            if entry not in definitions:
                definitions.append(entry)

        def handle_starttag(self, tag, attrs):
            element = dict(attrs=dict(attrs), offset=starts[self.getpos()[0]-1]+self.getpos()[1], text='')
            if re.fullmatch(r'h[1-6]', tag):
                self.heading = dict(element, level=int(tag[1]))
            elif tag == 'table':
                self.table = dict(headers=[])
            elif tag == 'tr' and self.table is not None:
                self.row = dict(element, cells=[])
            elif tag in ('th', 'td') and self.row is not None:
                self.cell = dict(element, tag=tag)

        def handle_data(self, data):
            if self.heading is not None: self.heading['text'] += data
            if self.cell is not None: self.cell['text'] += data

        def handle_endtag(self, tag):
            if re.fullmatch(r'h[1-6]', tag) and self.heading is not None:
                heading = self.heading
                name = heading['text'].strip()
                self.type_owners = {level: owner for level, owner in self.type_owners.items() if level < heading['level']}
                self.owner = self.type_owners[max(self.type_owners)] if self.type_owners else default_owner
                if self.enum_level is not None and heading['level'] <= self.enum_level:
                    self.enum_level = None
                datatype = name if name in declared else ''
                if not datatype:
                    match = next((match for pattern in type_headings if (match := pattern.fullmatch(name))), None)
                    if match and match[1].lower() not in boilerplate | section_adjectives:
                        datatype = match[1]
                    elif self.enum_level is not None and re.fullmatch(symbol, name):
                        datatype = name
                if datatype:
                    self.owner = datatype
                    self.type_owners[heading['level']] = datatype
                    self.type_anchor(heading, datatype)
                elif heading['level'] == 2 and name.endswith(' functions'):
                    self.owner = ''
                elif heading['level'] == 3:
                    member = re.sub(r' \(overload \d+\)$', '', name)
                    if re.fullmatch(r'[A-Za-z_]\w*(?:\.\w+)*', member) and member.lower() not in boilerplate:
                        self.anchor(heading, member if '.' in member or not self.owner else self.owner + '.' + member, 'member')
                if re.fullmatch(r'(?:' + symbol + r' )?Enumerations', name, re.I):
                    self.enum_level = heading['level']
                self.heading = None
            elif tag in ('th', 'td') and self.cell is not None:
                self.row['cells'].append(self.cell)
                self.cell = None
            elif tag == 'tr' and self.row is not None:
                cells = self.row['cells']
                if cells and cells[0]['tag'] == 'th' and not self.table['headers']:
                    self.table['headers'] = [c['text'].strip().lower() for c in cells]
                elif cells and self.table['headers'] and self.table['headers'][0] in ('name','method','property','member','function','event','constant','constructor','symbol','call'):
                    text = cells[0]['text'].strip()
                    # Only names/signatures in the API's first column define symbols.
                    namespace = ''
                    for match in re.finditer(r'(?:^|[/,]\s*)([A-Za-z_]\w*(?:\.\w+)*)(?=\s*\(|\s*(?:/|$))', text):
                        member = match[1]
                        if '.' in member: namespace = member.rsplit('.', 1)[0]
                        elif namespace: member = namespace + '.' + member
                        self.anchor(self.row, member if '.' in member or not self.owner else self.owner + '.' + member, 'member')
                self.row = None
            elif tag == 'table':
                self.table = None

    parser = DefinitionParser()
    parser.feed(source)
    if definitions:
        for offset, value in sorted(set(edits), reverse=True):
            source = source[:offset] + value + source[offset:]
        page['html'] = source
        page['definitions'] = definitions


def local_target(href, relative):
    decoded = unquote(html.unescape(href))
    require(not any(ord(c) < 32 for c in decoded) and '\\' not in decoded, 'unsafe link')
    parts = urlsplit(decoded)
    require(not parts.scheme and not parts.netloc and not parts.query and not parts.path.startswith('/'), 'nonlocal link')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), parts.path)) if parts.path else relative
    require(target != '..' and not target.startswith('../'), 'link escapes documentation root')
    return target, parts.fragment


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def render(source, relative, paths, assets, diagnostics):
    """Render only generated tags; authored HTML/CSS/scripts remain escaped text."""
    embedded_bytes = 0

    def link(label, href, image=False):
        nonlocal embedded_bytes
        try:
            decoded = unquote(html.unescape(href))
            require(not any(ord(c) < 32 for c in decoded) and '\\' not in decoded, 'unsafe link')
            external = urlsplit(decoded)
            if not image and external.scheme.lower() in ('https', 'http'):
                require(external.netloc and external.hostname and external.username is None and external.password is None, 'invalid external reference')
                # The reader copies external references on activation. There is
                # no network request, remote image, or automatic navigation.
                return '<a href="' + html.escape(href, quote=True) + '">' + html.escape(label) + '</a>'
            target, fragment = local_target(href, relative)
            if image:
                require(not fragment and target in assets, 'unsupported or missing local image')
                embedded_bytes += len(assets[target])
                require(embedded_bytes <= MAX_DOCS, 'page image budget exceeds 16 MiB')
                return '<img src="' + assets[target] + '" alt="' + html.escape(label, quote=True) + '">'
            require(target in paths, 'missing local page')
            return '<a href="' + html.escape(paths[target] + ('#' + quote(fragment, safe='-._~') if fragment else ''), quote=True) + '">' + html.escape(label) + '</a>'
        except ValueError as error:
            diagnostics.append({'page': relative, 'message': str(error), 'target': href[:2048]})
            return html.escape('[Image: ' + label + ']' if image else label)

    pattern = re.compile(r'(!?)\[([^\]\n]*)\]\(([^\s()]*)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*')
    def inline(value):
        output, cursor = [], 0
        for match in pattern.finditer(value):
            output.append(html.escape(value[cursor:match.start()]))
            image, label, href, code, strong, emphasis = match.groups()
            if label is not None:
                output.append(link(label, href, bool(image)))
            elif code is not None:
                output.append('<code>' + html.escape(code) + '</code>')
            elif strong is not None:
                output.append('<strong>' + html.escape(strong) + '</strong>')
            else:
                output.append('<em>' + html.escape(emphasis) + '</em>')
            cursor = match.end()
        output.append(html.escape(value[cursor:]))
        return ''.join(output)

    lines, output, headings = source.splitlines(), [], Counter()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.fullmatch(r'\s*(`{3,}|~{3,})([A-Za-z0-9_+.-]*)\s*', line)
        if fence:
            marker, language = fence.groups()
            body = []
            i += 1
            while i < len(lines) and not re.fullmatch(r'\s*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*', lines[i]):
                body.append(lines[i])
                i += 1
            attribute = ' class="language-' + language + '"' if language else ''
            output.append('<pre><code' + attribute + '>' + html.escape('\n'.join(body) + ('\n' if body else '')) + '</code></pre>')
        elif re.match(r'^#{1,6} ', line):
            heading, text = line.split(' ', 1)
            base = slug(text)
            count = headings[base]
            headings[base] += 1
            ident = base + ('-' + str(count) if count else '')
            output.append('<h{0} id="{1}">{2}</h{0}>'.format(len(heading), html.escape(ident, quote=True), inline(text)))
        elif i + 1 < len(lines) and '|' in line and re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[i + 1]):
            cells = lambda row: row.strip().strip('|').split('|')
            output.append('<table><thead><tr>' + ''.join('<th>' + inline(c.strip()) + '</th>' for c in cells(line)) + '</tr></thead><tbody>')
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                output.append('<tr>' + ''.join('<td>' + inline(c.strip()) + '</td>' for c in cells(lines[i])) + '</tr>')
                i += 1
            output.append('</tbody></table>')
            i -= 1
        elif re.match(r'^\s*(?:[-+*]|[0-9]+\.) ', line):
            ordered = bool(re.match(r'^\s*[0-9]+\.', line))
            tag = 'ol' if ordered else 'ul'
            expression = r'^\s*[0-9]+\. (.*)' if ordered else r'^\s*[-+*] (.*)'
            output.append('<' + tag + '>')
            while i < len(lines):
                item = re.match(expression, lines[i])
                if not item:
                    break
                output.append('<li>' + inline(item.group(1)) + '</li>')
                i += 1
            output.append('</' + tag + '>')
            i -= 1
        elif re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*', line):
            output.append('<hr>')
        elif line.startswith('> '):
            body = []
            while i < len(lines) and lines[i].startswith('> '):
                body.append(lines[i][2:])
                i += 1
            output.append('<blockquote><p>' + inline(' '.join(body)) + '</p></blockquote>')
            i -= 1
        elif line.strip():
            # Markdown source is commonly wrapped at 80 columns. Soft line
            # breaks belong to the same paragraph; the reader controls wrapping.
            body = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip():
                candidate = lines[i]
                if (re.match(r'^\s*(?:#{1,6} |`{3,}|~{3,}|[-+*] |[0-9]+\. |>{1} )', candidate)
                        or re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*', candidate)
                        or (i + 1 < len(lines) and '|' in candidate and
                            re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[i + 1]))):
                    break
                body.append(candidate.strip())
                i += 1
            output.append('<p>' + inline(' '.join(body)) + '</p>')
            i -= 1
        i += 1
    rendered = '\n'.join(output)
    parser = PlainText()
    parser.feed(rendered)
    return rendered, ' '.join(' '.join(parser.parts).split())


def image_uri(name, data):
    suffix = Path(name).suffix.lower()
    mime = None
    if suffix == '.png' and data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif suffix in ('.jpg', '.jpeg') and data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif suffix == '.gif' and data[:6] in (b'GIF87a', b'GIF89a'):
        mime = 'image/gif'
    elif suffix == '.webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    require(mime is not None, 'unsupported image type or signature: ' + name)
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


def read_package(path, claimed, diagnostics, expected_name=None):
    path = no_symlinks(path)
    require(path.stat().st_size <= MAX_ARCHIVE, 'archive exceeds 256 MiB')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        require(len(entries) <= MAX_ENTRIES, 'too many archive entries')
        names = set()
        for entry in entries:
            name = entry.filename
            require(name == entry.orig_filename and name and not name.startswith('/') and not any(c in name for c in '\\:') and not any(ord(c) < 32 for c in name) and all(p not in ('', '.', '..') for p in name.rstrip('/').split('/')), 'unsafe archive path')
            require(name.casefold() not in names, 'duplicate archive path')
            names.add(name.casefold())
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(mode in (0, stat.S_IFREG, stat.S_IFDIR), 'archive special file or symlink forbidden')
        def read(name, limit):
            entry = archive.getinfo(name)
            require(entry.file_size <= limit and entry.file_size <= max(1, entry.compress_size) * 200, 'entry size/compression limit: ' + name)
            require(not entry.flag_bits & 1, 'encrypted entry forbidden')
            with archive.open(entry) as stream:
                data = stream.read(limit + 1)
            require(len(data) <= limit, 'entry exceeds size limit')
            return data
        manifest = strict_json(read('manifest.json', MAX_ENTRY))
        require(isinstance(manifest, dict) and type(manifest.get('schema')) is int and manifest['schema'] == 1, 'invalid manifest schema')
        name, version = manifest.get('name'), manifest.get('version')
        require(isinstance(name, str) and PACKAGE.fullmatch(name), 'invalid package name')
        if expected_name is not None and name != expected_name:
            claimed.add(expected_name)
            raise ValueError('Locked archive manifest identity mismatch')
        if name in claimed:
            return [], None
        claimed.add(name)
        require(isinstance(version, str) and len(version) <= 128 and VERSION.fullmatch(version), 'invalid package version')
        core_and_pre, _, build = version.partition('+')
        _, dash, prerelease = core_and_pre.partition('-')
        require(not build or all(build.split('.')), 'empty version build identifier')
        require(not dash or all(part and (not part.isdigit() or part == '0' or not part.startswith('0')) for part in prerelease.split('.')), 'invalid prerelease identifier')
        relevant = [e for e in entries if e.filename.startswith(DOCS) and not e.is_dir()]
        require(sum(e.file_size for e in relevant) <= MAX_DOCS, 'package documentation exceeds 16 MiB')
        prefix = '/package/' + name + '/' + quote(version, safe='') + '/'
        source_id = 'package:' + name
        documents, assets = {}, {}
        for entry in sorted(relevant, key=lambda e: e.filename):
            relative = entry.filename[len(DOCS):]
            try:
                if relative.endswith('.md'):
                    documents[relative] = metadata(read(entry.filename, MAX_ENTRY).decode('utf-8-sig'))
                elif Path(relative).suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                    assets[relative] = image_uri(relative, read(entry.filename, MAX_IMAGE))
            except (ValueError, UnicodeError) as error:
                diagnostics.append({'path': str(path), 'page': relative, 'message': str(error)})
        paths = {relative: prefix + quote(relative, safe='/') for relative in documents}
        pages = []
        rendered_bytes = 0
        for relative, (meta, body) in sorted(documents.items(), key=lambda item: (item[1][0].get('order', 0), item[0])):
            rendered, text = render(body, relative, paths, assets, diagnostics)
            rendered_bytes += len(rendered.encode('utf-8'))
            require(rendered_bytes <= 32 * MIB, 'rendered package documentation exceeds 32 MiB')
            heading = re.search(r'^# (.+)$', body, re.MULTILINE)
            title = meta.get('title', heading.group(1).strip() if heading else Path(relative).stem)
            if 'types' in meta:
                valid_types = []
                for datatype in meta['types']:
                    if 'id="' + slug(datatype) + '"' in rendered:
                        valid_types.append(datatype)
                    else:
                        diagnostics.append({'page': relative, 'message': 'missing type anchor', 'target': datatype})
                meta = dict(meta, types=valid_types)
            pages.append(dict(path=paths[relative], title=title, section='packages', relative=relative,
                              source=path.as_uri() + '!/' + quote(DOCS + relative, safe='/'),
                              html=rendered, text=text, sourceId=source_id, sourceName=name,
                              package=name, version=version, **{k: v for k, v in meta.items() if k != 'title'}))
        by_relative = {page['relative']: page for page in pages}
        dependencies = sorted({name for page in pages for name in page.get('dependencies', [])})
        for page in pages:
            if dependencies:
                page['dependencies'] = dependencies
            related = []
            for href in page.get('related', []):
                try:
                    target, fragment = local_target(href, page['relative'])
                    require(target in paths, 'missing related topic')
                    value = paths[target] + ('#' + quote(fragment, safe='-._~') if fragment else '')
                    if value != page['path'] and value not in related:
                        related.append(value)
                except ValueError as error:
                    diagnostics.append({'page': page['relative'], 'message': str(error), 'target': href})
            if 'related' in page:
                page['related'] = related
            root = by_relative.get('index.md')
            crumbs = [dict(title=root['title'] if root else name, path=root['path'] if root else '')]
            parts = page['relative'].split('/')[:-1]
            for depth, folder in enumerate(parts, 1):
                parent = by_relative.get('/'.join(parts[:depth]) + '/index.md')
                crumbs.append(dict(title=parent['title'] if parent else folder, path=parent['path'] if parent else ''))
            if crumbs[-1]['path'] != page['path']:
                crumbs.append(dict(title=page['title'], path=page['path']))
            page['breadcrumbs'] = crumbs
        return pages, dict(id=source_id, name=name, kind='package', package=name, version=version,
                           origin=str(path), pageCount=len(pages))


def locked_archives(project, diagnostics):
    """Prefer the installed closure selected by BPM; unmanaged packages still work."""
    lock_path = Path(project) / 'bpm.lock'
    if not lock_path.exists():
        return {}
    try:
        no_symlinks(lock_path)
        with lock_path.open('rb') as stream:
            raw = stream.read(8 * MIB + 1)
        require(len(raw) <= 8 * MIB, 'BPM lock exceeds 8 MiB')
        lock = strict_json(raw)
        require(isinstance(lock, dict) and lock.get('schema') == 1, 'invalid BPM lock schema')
        graph = lock.get('packages')
        require(isinstance(graph, dict), 'invalid BPM lock packages')
        install = lock.get('install', list(graph))
        require(isinstance(install, list) and len(install) <= 4096, 'invalid BPM install closure')
        expected = {}
        for name in install:
            require(isinstance(name, str) and PACKAGE.fullmatch(name), 'invalid BPM package name')
            entry = graph.get(name)
            require(isinstance(entry, dict) and entry.get('name') == name, 'invalid BPM package identity')
            version, digest = entry.get('version'), entry.get('sha256')
            require(isinstance(version, str) and len(version) <= 128 and VERSION.fullmatch(version), 'invalid BPM package version')
            require(isinstance(digest, str) and re.fullmatch('[0-9a-fA-F]{64}', digest), 'invalid BPM archive digest')
            filename = name + '-' + version + '.buipkg'
            require(filename not in expected, 'duplicate BPM install identity')
            expected[filename] = (name, digest.lower())
        return expected
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as error:
        diagnostics.append({'path': str(lock_path), 'message': str(error)})
        return {}


def discover(project, snapshot, global_dir=None):
    with open(snapshot, 'rb') as stream:
        raw = stream.read(MAX_OUTPUT + 1)
    require(len(raw) <= MAX_OUTPUT, 'upstream snapshot exceeds 128 MiB')
    result = strict_json(raw)
    require(isinstance(result, dict) and isinstance(result.get('pages'), list), 'invalid upstream snapshot')
    # A unified snapshot may itself be reused as input: never retain stale packages.
    require(all(isinstance(p, dict) for p in result['pages']), 'invalid upstream page')
    require(all(all(isinstance(p.get(k), str) for k in ('path', 'title', 'section', 'relative', 'source', 'html', 'text')) for p in result['pages']), 'invalid upstream page')
    pages = [p for p in result['pages'] if not p.get('package') and not p['path'].startswith('/package/')]
    diagnostics, claimed = [], set()
    expected = locked_archives(project, diagnostics)
    local_directory = Path(project) / 'packages'
    for filename, (name, _) in expected.items():
        if not (local_directory / filename).exists():
            claimed.add(name)
            diagnostics.append({'path': str(local_directory / filename), 'message': 'Locked package is not installed; run bpm sync'})
    package_bytes = 0
    sources = [dict(id='upstream', name='Buoy', kind='upstream', origin=result.get('source', str(snapshot)), pageCount=len(pages))]
    for directory in (local_directory, Path(global_dir) if global_dir is not None else global_directory()):
        try:
            directory = no_symlinks(directory)
            if not directory.exists():
                continue
            paths = []
            for path in directory.iterdir():
                if path.suffix == '.buipkg':
                    paths.append(path)
                    require(len(paths) <= 4096, 'too many package archives')
            local = directory == Path(os.path.abspath(local_directory))
            for path in sorted(paths, key=lambda p: (0 if local and p.name in expected else 1, p.name)):
                try:
                    locked = expected.get(path.name) if local else None
                    if locked:
                        name, digest = locked
                        try:
                            no_symlinks(path)
                            require(path.stat().st_size <= MAX_ARCHIVE, 'archive exceeds 256 MiB')
                            with path.open('rb') as stream:
                                hasher = hashlib.sha256()
                                for chunk in iter(lambda: stream.read(MIB), b''):
                                    hasher.update(chunk)
                                actual = hasher.hexdigest()
                            require(actual == digest, 'Locked package SHA-256 mismatch; run bpm verify')
                        except (ValueError, OSError):
                            claimed.add(name)
                            raise
                    additions, source = read_package(path, claimed, diagnostics, locked[0] if locked else None)
                    if locked and source is not None:
                        require(source['package'] == locked[0], 'Locked archive manifest identity mismatch')
                    package_bytes += sum(len(json.dumps(p).encode('utf-8')) for p in additions)
                    require(package_bytes <= 64 * MIB, 'combined package documentation exceeds 64 MiB')
                    pages.extend(additions)
                    if source is not None:
                        sources.append(source)
                except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError, RecursionError, NotImplementedError, EOFError, zlib.error) as error:
                    if locked:
                        claimed.add(locked[0])
                    diagnostics.append({'path': str(path), 'message': str(error)})
        except (ValueError, OSError) as error:
            diagnostics.append({'path': str(directory), 'message': str(error)})
    index = {}
    for number, page in enumerate(pages):
        index_definitions(page)
        searchable = ' '.join([page['title'], page['text'], page.get('summary', ''), page.get('category', ''), page.get('package', ''), *page.get('keywords', []), *page.get('symbols', []), *[d['name'] for d in page.get('definitions', [])]])
        for token in sorted(set(re.findall(r'\w+', searchable.lower()))):
            index.setdefault(token, []).append(number)
    valid_paths = {p['path'] for p in pages}
    result.update(pages=pages, index=index, counts=dict(Counter(p['section'] for p in pages)), sources=sources,
                  diagnostics=diagnostics, captured=datetime.now(timezone.utc).isoformat(),
                  aliases={k: v for k, v in result.get('aliases', {}).items() if v in valid_paths})
    result.setdefault('missing', [])
    if result.get('startPath') not in valid_paths:
        result['startPath'] = pages[0]['path'] if pages else ''
    return result


def write_snapshot(output, result):
    data = json.dumps(result, ensure_ascii=True, allow_nan=False).encode('utf-8') + b'\n'
    require(len(data) <= MAX_OUTPUT, 'unified snapshot exceeds 128 MiB')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=output.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--global-dir', type=Path)
    args = parser.parse_args()
    try:
        result = discover(args.project, args.snapshot, args.global_dir)
        write_snapshot(args.output, result)
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({'output': str(args.output), 'pages': len(result['pages']), 'sources': len(result['sources']), 'diagnostics': result['diagnostics']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
