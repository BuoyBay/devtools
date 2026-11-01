#!/usr/bin/env python3
"""Create signed updatekit manifests using an offline Ed25519 key and OpenSSL 3."""
import argparse
import base64
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from pathlib import Path
import subprocess
import tempfile
import time


def command(arguments, **kwargs):
    try:
        return subprocess.run(['openssl', *arguments], check=True, capture_output=True, **kwargs).stdout
    except subprocess.CalledProcessError as error:
        # stdout can contain private key material from genpkey.
        detail = error.stderr or b''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        detail = detail.strip() or f'exit status {error.returncode}'
        raise RuntimeError(f'OpenSSL failed: {detail}') from error


def public_key(private):
    der = command(['pkey', '-in', str(private), '-pubout', '-outform', 'DER'])
    prefix = bytes.fromhex('302a300506032b6570032100')
    if len(der) != len(prefix) + 32 or not der.startswith(prefix):
        raise ValueError('An Ed25519 private key is required')
    return der[len(prefix):].hex()


def write_new(filename, data, mode=0o644):
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def sign(private, payload):
    # OpenSSL's Ed25519 signer requires a seekable input, not stdin.
    with tempfile.TemporaryDirectory(prefix='updatekit-sign-') as directory:
        source = Path(directory) / 'payload'
        source.write_bytes(payload)
        signature = command(['pkeyutl', '-sign', '-inkey', str(private), '-rawin', '-in', str(source)])
    if len(signature) != 64:
        raise ValueError('Expected an Ed25519 signature')
    return {'payload': base64.b64encode(payload).decode(), 'signature': signature.hex()}


def valid_semver(value):
    match = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', value)
    if not match:
        return False
    if any(int(part) > 18446744073709551615 for part in match.group(1, 2, 3)):
        return False
    return not match.group(4) or all(not (part.isdigit() and len(part) > 1 and part.startswith('0')) for part in match.group(4).split('.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    keygen = actions.add_parser('keygen', help='Create a new private PEM and public hex key; never overwrites files')
    keygen.add_argument('--private-key', type=Path, required=True)
    keygen.add_argument('--public-key', type=Path, required=True)
    create = actions.add_parser('manifest', help='Hash assets and sign exact manifest bytes')
    create.add_argument('--private-key', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--app-id', required=True)
    create.add_argument('--version', required=True)
    create.add_argument('--channel', default='stable')
    create.add_argument('--sequence', type=int, required=True)
    create.add_argument('--expires', type=int, help='Unix seconds; defaults to 30 days from now')
    create.add_argument('--notes-file', type=Path)
    create.add_argument('--asset', nargs=6, action='append', required=True,
                        metavar=('PLATFORM', 'ARCH', 'FORMAT', 'ENTRY', 'FILE', 'HTTPS_URL'),
                        help='Repeat per platform; format binary, zip or tar.gz; entry is empty for binary')
    args = parser.parse_args()
    if args.action == 'keygen':
        private = command(['genpkey', '-algorithm', 'ED25519'])
        write_new(args.private_key, private, 0o600)
        write_new(args.public_key, (public_key(args.private_key) + '\n').encode())
        print(f'Wrote private key to {args.private_key}; store it offline. Public key: {args.public_key}')
        return
    if not valid_semver(args.version):
        parser.error('--version must be strict SemVer, for example 1.2.3 or 1.2.3-rc.1')
    if not args.app_id.strip() or not args.channel.strip():
        parser.error('--app-id and --channel must not be empty')
    if not 0 <= args.sequence <= 9007199254740991:
        parser.error('--sequence must be in 0..2^53-1')
    expires = args.expires if args.expires is not None else int(time.time()) + 30 * 86400
    if expires <= time.time() or expires > 9007199254740991:
        parser.error('--expires must be in the future and at most 2^53-1')
    assets = []
    seen = set()
    for platform_name, arch, archive_format, entry, filename, url in args.asset:
        if platform_name not in ('macos', 'windows', 'linux') or arch not in ('arm64', 'x86_64'):
            parser.error('Unsupported platform or architecture')
        if archive_format not in ('binary', 'zip', 'tar.gz'):
            parser.error('Format must be binary, zip or tar.gz')
        if archive_format == 'binary' and entry:
            parser.error('Binary assets require an empty entry')
        if (platform_name, arch) in seen:
            parser.error('Duplicate platform/architecture asset')
        seen.add((platform_name, arch))
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            parser.error('Publish asset URLs over HTTPS without credentials or fragments')
        source = Path(filename)
        with source.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        assets.append({'platform': platform_name, 'arch': arch, 'format': archive_format,
                       'entry': entry, 'url': url, 'size': source.stat().st_size, 'sha256': digest})
    payload = json.dumps({'schema': 1, 'app_id': args.app_id, 'sequence': args.sequence,
                          'expires': expires, 'releases': [{'version': args.version, 'channel': args.channel,
                          'notes': args.notes_file.read_text() if args.notes_file else '', 'assets': assets}]},
                         ensure_ascii=False, separators=(',', ':')).encode()
    if len(payload) > 3 * 1024 * 1024 - 1024:
        parser.error('Manifest exceeds the client metadata limit')
    envelope = sign(args.private_key, payload)
    encoded = (json.dumps(envelope, separators=(',', ':')) + '\n').encode()
    if len(encoded) > 4 * 1024 * 1024:
        parser.error('Envelope exceeds the client metadata limit')
    write_new(args.output, encoded)
    print(f'Wrote {args.output}; pin public key {public_key(args.private_key)} in your application')


if __name__ == '__main__':
    main()
