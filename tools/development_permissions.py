"""Build-time selection of the development-only credential transport.

Only staged consumer sources change. The sealed Keychain and SecretStore
packages and the repository application sources keep their production behavior.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('development', 'production', 'debug', 'release')
HELPER_RELATIVE = Path('Applications/Buoy Development Services.app/Contents/MacOS/BuoyDevelopmentServices')


def uses_broker(profile, target):
    if profile not in PROFILES:
        raise ValueError('Unknown permission build profile: ' + str(profile))
    return profile in ('development', 'debug') and target.endswith('-darwin')


def stage_transport(work, target, profile='development', home=None):
    if not uses_broker(profile, target):
        if (Path(work) / 'DevelopmentKeychain.bui').exists():
            raise RuntimeError('Refusing to reuse a development credential consumer for a direct-storage build')
        return False
    work = Path(work)
    helper = Path(home or Path.home()) / HELPER_RELATIVE
    fingerprint = hashlib.sha256(helper.read_bytes()).hexdigest() if helper.is_file() else 'UNCONFIGURED'
    source = (ROOT / 'apps/shared/development/DevelopmentKeychain.bui').read_text()
    source = source.replace('"UNCONFIGURED"', '"' + fingerprint + '"', 1)
    (work / 'DevelopmentKeychain.bui').write_text(source)
    return True


def stage_studio(work, target, profile='development', home=None):
    if not stage_transport(work, target, profile, home):
        return
    path = Path(work) / 'StudioCredentials.bui'
    source = path.read_text()
    before = 'New SecretRecordStore("com.buoy.studio.chatgpt", hostID)'
    after = 'New SecretRecordStore("com.buoy.development.com.buoy.studio.chatgpt", hostID, New DevelopmentSecretRecordItems("com.buoy.studio.chatgpt"), True)'
    if source.count(before) != 1:
        raise RuntimeError('Studio credential constructor changed; update development transport integration')
    path.write_text('Import DevelopmentKeychain\n' + source.replace(before, after))
