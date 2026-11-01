#!/usr/bin/env python3
"""Read package design DATA only; never extract or execute package contents.

Studio startup: python3 tools/discover_layout_packages.py --project PROJECT
Then: New LayoutDiscovery().DiscoverDirectory(PROJECT, catalog).
The cache is rebuilt atomically on every run (including removals). Python 3's
standard library is the only dependency; no ZIP/native dependency in layoutkit.
Local packages take precedence over installed globals, matching the compiler.
"""
import argparse
import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile
import zlib

RESOURCE = 'resources/buoy-designer/plugin.json'
CACHE = '.buoy/designer/index-v1.json'
MAX_JSON = 1024 * 1024
MAX_INDEX = 8 * MAX_JSON
SYMBOL = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
PACKAGE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?\Z')
QUALIFIED = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z')
ICON_PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[Pp][Nn][Gg]\Z')
MAX_ICON = 64 * 1024
MAX_ICON_SIDE = 512
ICON_DATA_CHARS = 4 * ((MAX_ICON + 2) // 3)
PNG_HEADER = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
# One op/field vocabulary for renderer and icon recipes (LayoutDiscovery.bui).
RECIPE_OPS = ('fillRect', 'fillRoundedRect', 'strokeRect', 'strokeRoundedRect', 'fillOval', 'strokeOval', 'text')
RECIPE_FIELDS = {'op', 'color', 'darkColor', 'x', 'y', 'inset', 'width', 'height', 'radius', 'lineWidth', 'size',
                 'prefix', 'suffix', 'property', 'whenProperty', 'equals', 'elseColor'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def strict_json(data):
    require(len(data) <= MAX_JSON, 'metadata exceeds 1 MiB')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    def reject(value):
        raise ValueError('non-finite JSON number: ' + value)
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=reject)
    def walk(node, depth=0):
        require(depth <= 24, 'metadata nesting exceeds 24')
        if isinstance(node, dict):
            require(len(node) <= 1024 and all(len(key) <= 256 for key in node), 'metadata object/key too large')
            for key, child in node.items():
                # Embedded icon PNGs are the one long string (validated by png()).
                if key == 'iconImageData' and isinstance(child, str):
                    require(len(child) <= ICON_DATA_CHARS, 'icon image data too large')
                    continue
                walk(child, depth + 1)
        elif isinstance(node, list):
            require(len(node) <= 16384, 'metadata array too large')
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, str):
            require(len(node) <= 4096, 'metadata string too large')
        elif isinstance(node, float):
            require(math.isfinite(node), 'non-finite number')
    walk(value)
    return value


def text(value, limit=256):
    return isinstance(value, str) and len(value) <= limit and all(ord(c) >= 32 for c in value)


def expression(kind, value, package):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    if kind == 'string':
        try:
            return isinstance(json.loads(value), str) and bool(re.fullmatch(r'"(?:[^"\\\x00-\x1f]|\\[nrt"\\])*"', value))
        except ValueError:
            return False
    if kind == 'boolean':
        return value in ('True', 'False')
    if kind == 'enum':
        return bool(QUALIFIED.fullmatch(value)) and value.startswith(package + '.')
    if kind == 'integer':
        return bool(re.fullmatch(r'-?[0-9]+', value)) and -2**63 <= int(value) < 2**63
    if kind == 'double':
        return bool(re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?', value)) and math.isfinite(float(value))
    return False


def validate_recipe(recipe, props, package, label):
    require(isinstance(recipe, dict) and type(recipe.get('version')) is int and recipe['version'] == 1, 'unsupported ' + label + ' version')
    steps = recipe.get('steps')
    require(isinstance(steps, list) and 0 < len(steps) <= 64, 'invalid ' + label + ' steps')
    for s in steps:
        require(isinstance(s, dict) and s.get('op') in RECIPE_OPS, 'unsupported ' + label + ' operation')
        require(not (set(s) - RECIPE_FIELDS), 'unknown ' + label + ' field')
        for key in ('color', 'elseColor', 'darkColor'):
            if key in s:
                require(isinstance(s[key], list) and len(s[key]) == 3 and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in s[key]), 'invalid RGB color')
        require('color' in s, label + ' color required')
        for key in ('x', 'y', 'inset', 'width', 'height', 'radius', 'size'):
            if key in s:
                require(type(s[key]) in (int, float) and math.isfinite(s[key]) and 0 <= s[key] <= 1024, 'invalid ' + label + ' geometry')
        if 'lineWidth' in s:
            require(type(s['lineWidth']) in (int, float) and math.isfinite(s['lineWidth']) and 0 < s['lineWidth'] <= 64, 'invalid ' + label + ' line width')
        for key in ('prefix', 'suffix'):
            require(key not in s or text(s[key], 1024), 'invalid ' + label + ' text')
        for key in ('property', 'whenProperty'):
            require(key not in s or s[key] in [v['name'] for v in props], 'unknown ' + label + ' property')
        if 'whenProperty' in s:
            prop = next(v for v in props if v['name'] == s['whenProperty'])
            require(expression(prop['kind'], s.get('equals'), package) and 'elseColor' in s, 'invalid ' + label + ' condition')


def png(data):
    """Validate icon PNG bytes without decoding pixels: size, signature, IHDR bounds."""
    require(33 <= len(data) <= MAX_ICON, 'icon image must be 33 bytes..64 KiB')
    require(data[:16] == PNG_HEADER, 'icon image must be a PNG')
    width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    require(1 <= width <= MAX_ICON_SIDE and 1 <= height <= MAX_ICON_SIDE, 'icon image must be at most 512x512')
    return data


SOURCE_RESERVED = frozenset('abstract alias class const declare dim enum export final flags function get implements import inherits interface lib operator overrides packed paramarray private property protected public readonly set shared structure sub var array boolean double integer optional ptr result single string break case continue do downto else elseif end exit for if loop next return select step then to until wend while and as ctype each in is isa isnot me mod new not of or super trycast weak xor false nil none ok some true print sequential stderr stdin stdout err math path random addressof assigns async await block byref call catch cfstringref closure condition constant cstring currency delegate event extension finally inline internal lambda lazy localization mainthread module mutex namespace package partial raiseevent runloop selector synclock task thread throw treat try using with int8 int16 int32 int64 uint8 uint16 uint32 uint64 handle safearea priority constraints control window view'.split())
GEOMETRY_MEMBERS = frozenset(('frame x y width height left top right bottom bounds size position constraints layout lockleft lockright locktop lockbottom leadinganchor trailinganchor topanchor bottomanchor widthanchor heightanchor centerxanchor centeryanchor baselineanchor').split())


def writable_property_name(name):
    return isinstance(name, str) and bool(SYMBOL.fullmatch(name)) and name.casefold() not in SOURCE_RESERVED | GEOMETRY_MEMBERS


def validate_descriptor(d, manifest=None):
    require(isinstance(d, dict), 'descriptor must be an object')
    require(d.get('schema') == 'buoy.designer.plugin' and type(d.get('schemaVersion')) is int and d['schemaVersion'] == 1, 'unsupported descriptor schema/version')
    p = d.get('package', {})
    require(isinstance(p, dict) and isinstance(p.get('name'), str) and bool(PACKAGE.fullmatch(p['name'])), 'invalid package name')
    require(isinstance(p.get('version'), str) and bool(VERSION.fullmatch(p['version'])), 'invalid package version')
    require(text(p.get('provenance'), 512) and bool(p['provenance']), 'package provenance required')
    if manifest is not None:
        require(p['name'] == manifest.get('name') and p['version'] == manifest.get('version'), 'descriptor/manifest identity mismatch')
    controls = d.get('controls')
    require(isinstance(controls, list) and 0 < len(controls) <= 128, 'expected 1..128 controls')
    seen, editors = set(), set()
    for c in controls:
        require(isinstance(c, dict), 'invalid control')
        name = c.get('typeName', '')
        require(isinstance(name, str) and name.startswith(p['name'] + '.') and bool(SYMBOL.fullmatch(name[len(p['name']) + 1:])), 'control must use package.Type identity')
        require(name.casefold() not in seen, 'duplicate control symbol')
        seen.add(name.casefold())
        require(text(c.get('displayName')) and c['displayName'], 'invalid displayName')
        imp = c.get('sourceImport', '')
        require(isinstance(imp, str) and imp.startswith(p['name'] + '.') and bool(QUALIFIED.fullmatch(imp)) and len(imp) <= 256, 'invalid sourceImport')
        for key in ('defaultWidth', 'defaultHeight'):
            require(type(c.get(key)) is int and 1 <= c[key] <= 16384, 'invalid ' + key)
        props = c.get('properties')
        require(isinstance(props, list) and len(props) <= 128, 'invalid properties')
        names = set()
        for prop in props:
            require(isinstance(prop, dict) and writable_property_name(prop.get('name')), 'invalid property name')
            require(prop['name'].casefold() not in names, 'duplicate property symbol')
            names.add(prop['name'].casefold())
            require(expression(prop.get('kind'), prop.get('defaultExpression'), p['name']), 'unsafe default expression')
            for key in ('displayName', 'category'):
                require(key not in prop or text(prop[key]), 'invalid property ' + key)
            require('metadata' not in prop or isinstance(prop['metadata'], dict), 'invalid metadata')
            opts = prop.get('options', [])
            require(isinstance(opts, list) and len(opts) <= 64 and all(isinstance(v, str) for v in opts) and len(set(opts)) == len(opts), 'invalid choices')
            require(all(expression(prop['kind'], v, p['name']) for v in opts), 'unsafe choice expression')
            require(not opts or prop['defaultExpression'] in opts, 'default must be a choice')
            require(prop['kind'] != 'enum' or bool(opts), 'enum needs choices')
            editor = prop.get('editorId', '')
            if editor:
                require(text(editor) and editor.startswith(name + '.') and bool(QUALIFIED.fullmatch(editor)) and opts, 'choice editor id must be control-qualified')
                require(editor.casefold() not in editors, 'duplicate editor symbol')
                editors.add(editor.casefold())
        validate_recipe(c.get('renderer'), props, p['name'], 'renderer')
        for key, limit in (('category', 64), ('description', 256)):
            if key in c:
                require(text(c[key], limit) and c[key].strip(), 'invalid control ' + key)
        if 'icon' in c:
            icon = c['icon']
            if isinstance(icon, str):
                require(text(icon, 64) and icon, 'invalid icon glyph name')
            else:
                validate_recipe(icon, props, p['name'], 'icon')
        if 'iconImage' in c:
            require(isinstance(c['iconImage'], str) and len(c['iconImage']) <= 128 and bool(ICON_PATH.fullmatch(c['iconImage']))
                    and all(part not in ('.', '..') for part in c['iconImage'].split('/')), 'invalid iconImage path')
        if 'iconImageData' in c:
            require(isinstance(c['iconImageData'], str), 'invalid iconImageData')
            try:
                png(base64.b64decode(c['iconImageData'], validate=True))
            except binascii.Error:
                raise ValueError('invalid iconImageData encoding')
    return d


def read_archive(path, claimed=None, directory_claimed=None):
    require(path.stat().st_size <= 256 * MAX_JSON, 'archive exceeds 256 MiB')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        require(len(entries) <= 16384, 'too many archive entries')
        names = set()
        for entry in entries:
            name = entry.filename
            require(name and not name.startswith('/') and '\\' not in name and ':' not in name and '\x00' not in name and all(p not in ('.', '..') for p in name.rstrip('/').split('/')), 'unsafe archive path')
            require(name.casefold() not in names, 'duplicate archive path')
            names.add(name.casefold())
            require(not stat.S_ISLNK(entry.external_attr >> 16), 'archive symlink forbidden')
        def read(name):
            entry = archive.getinfo(name)
            require(entry.file_size <= MAX_JSON and entry.file_size <= max(1, entry.compress_size) * 200, 'metadata ZIP bomb')
            require(not entry.flag_bits & 1, 'encrypted metadata forbidden')
            with archive.open(entry) as stream:
                data = stream.read(MAX_JSON + 1)
            return strict_json(data)
        manifest = read('manifest.json')
        require(isinstance(manifest, dict), 'invalid manifest')
        require(type(manifest.get('schema')) is int and manifest['schema'] == 1, 'unsupported package manifest schema')
        name = manifest.get('name', '')
        require(isinstance(name, str) and bool(PACKAGE.fullmatch(name)), 'invalid manifest name')
        if directory_claimed is not None:
            require(name not in directory_claimed, 'ambiguous duplicate package identity: ' + name)
            directory_claimed.add(name)
        if claimed is not None:
            if name in claimed:
                return manifest, None, []
            # A broken local descriptor must still shadow global metadata for
            # the same compiler-resolved package identity.
            claimed.add(name)
        descriptor = validate_descriptor(read(RESOURCE), manifest) if RESOURCE in archive.namelist() else None
        warnings = []
        if descriptor is not None:
            embed_icons(archive, descriptor, warnings)
        return manifest, descriptor, warnings


def embed_icons(archive, descriptor, warnings):
    """Copy each control's iconImage PNG (data only) into iconImageData so Buoy can
    decode it from the index without ZIP support. A missing image is a warning:
    the declarative or generic icon still applies."""
    base = RESOURCE.rsplit('/', 1)[0] + '/'
    names = set(archive.namelist())
    for control in descriptor['controls']:
        location = control.get('iconImage')
        if location is None or 'iconImageData' in control:
            continue
        member = base + location
        if member not in names:
            warnings.append({'typeName': control['typeName'], 'message': 'iconImage not found in package: ' + member})
            continue
        entry = archive.getinfo(member)
        require(entry.file_size <= MAX_ICON and entry.file_size <= max(1, entry.compress_size) * 200, 'icon image ZIP bomb or too large')
        require(not entry.flag_bits & 1, 'encrypted icon image forbidden')
        with archive.open(entry) as stream:
            data = stream.read(MAX_ICON + 1)
        control['iconImageData'] = base64.b64encode(png(data)).decode('ascii')


def global_directory():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/buoy/packages'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'buoy/packages'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'buoy/packages'


def discover(project, global_dir=None):
    project = Path(project).resolve()
    result = {'schema': 'buoy.designer.index', 'schemaVersion': 1, 'packages': [], 'errors': []}
    claimed = set()
    # Only compiler package roots; no recursive traversal or package hooks.
    for directory in (project / 'packages', Path(global_dir) if global_dir is not None else global_directory()):
        if not directory.is_dir():
            continue
        paths = sorted(directory.glob('*.buipkg'))
        require(len(paths) <= 4096, 'too many package archives')
        directory_claimed = set()
        directory_loaded = set()
        for path in paths:
            try:
                manifest, d, warnings = read_archive(path, claimed, directory_claimed)
                # Claim even packages without a design resource: a global design
                # resource must never supply metadata for a different local build.
                name = manifest['name']
                require(isinstance(name, str) and bool(PACKAGE.fullmatch(name)), 'invalid manifest name')
                if d is None:
                    continue
                raw = path.read_bytes()
                entry = {'descriptor': d, 'origin': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}
                if warnings:
                    entry['warnings'] = warnings
                result['packages'].append(entry)
                directory_loaded.add(name)
            except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError, RecursionError, OverflowError, NotImplementedError, EOFError, zlib.error) as error:
                message = str(error)
                if message.startswith('ambiguous duplicate package identity: '):
                    identity = message.split(': ', 1)[1]
                    # Refuse ambiguity rather than guessing the compiler's winner.
                    # Keep the identity claimed so global metadata cannot fill it.
                    # Resolved origins can be outside this directory (symlinks).
                    # Membership comes from this scan, never the target's parent.
                    if identity in directory_loaded:
                        result['packages'] = [entry for entry in result['packages']
                                              if entry['descriptor']['package']['name'] != identity]
                        directory_loaded.discard(identity)
                result['errors'].append({'path': str(path), 'message': message})
    require(len(json.dumps(result).encode()) <= MAX_INDEX, 'index exceeds 8 MiB')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--global-dir', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = discover(args.project, args.global_dir)
    output = args.output or args.project / CACHE
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=output.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(result, stream, ensure_ascii=True, allow_nan=False)
        stream.write('\n')
    os.replace(temporary, output)
    print(json.dumps({'index': str(output), 'packages': len(result['packages']), 'errors': result['errors']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
