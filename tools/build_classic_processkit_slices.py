#!/usr/bin/env python3
"""Build isolated ProcessKit ARM64 slices; never install, seal globally or fetch tools."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'local/classic-roadmap/processkit-slices'
IMAGE = 'buoy:font-arm-validation'
ZIG = Path.home() / '.cache/buoy-compressionkit-arm/zig-x86_64-linux-0.14.1/zig'
TRIPLES = {'arm64-linux': 'aarch64-unknown-linux-gnu',
           'arm64-windows': 'aarch64-pc-windows-msvc'}

# This executable is both the ARM64 test driver and the ARM64 spawned fixture.
# QEMU is explicit: no host binfmt registration or ARM hardware is assumed.
HARNESS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
extern void *bpk_new(const char *, const char *);
extern int64_t bpk_arg(void *, const char *), bpk_env(void *, const char *, const char *);
extern int64_t bpk_own_tree(void *), bpk_start(void *), bpk_poll(void *), bpk_exit(void *);
extern int64_t bpk_write(void *, const void *, int64_t, int64_t);
extern int64_t bpk_read(void *, int64_t, void *, int64_t), bpk_eof(void *, int64_t);
extern int64_t bpk_terminate_tree(void *);
extern void bpk_close_input(void *), bpk_delete(void *);
static double now(void) {
    struct timespec t; assert(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void *spawn(const char *self, const char *mode, int owned) {
    void *p = bpk_new("/usr/bin/qemu-aarch64", ""); assert(p);
    assert(bpk_arg(p, "-L") && bpk_arg(p, "/") && bpk_arg(p, self) && bpk_arg(p, mode));
    if (owned) assert(bpk_own_tree(p));
    assert(bpk_env(p, "BPK_SLICE_ENV", "child-value"));
    assert(bpk_start(p)); return p;
}
static int dead(pid_t pid) {
    char path[80], line[512], *end;
    snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
    FILE *f = fopen(path, "r");
    if (!f) return errno == ENOENT;
    assert(fgets(line, sizeof(line), f)); fclose(f);
    end = strrchr(line, ')'); return end && end[2] == 'Z';
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "echo")) {
        unsigned char b[32]; ssize_t n = read(0, b, sizeof(b));
        assert(n == 5 && !memcmp(b, "A\0B\xffZ", 5));
        assert(!strcmp(getenv("BPK_SLICE_ENV"), "child-value"));
        assert(write(1, b, (size_t)n) == n); assert(write(2, "error-only", 10) == 10); return 23;
    }
    if (argc == 2 && !strcmp(argv[1], "tree")) {
        pid_t child = fork(); assert(child >= 0);
        if (!child) { for (;;) pause(); }
        printf("%ld\n", (long)child); fflush(stdout); for (;;) pause();
    }
    assert(argc == 1); assert(sizeof(void *) == 8);
    assert(setenv("BPK_SLICE_ENV", "parent-value", 1) == 0);
    void *p = spawn(argv[0], "echo", 0);
    assert(bpk_write(p, "A\0B\xffZ", 5, 0) == 5); bpk_close_input(p);
    unsigned char out[64], err[64]; int64_t no = 0, ne = 0; double until = now() + 15;
    do {
        int64_t n = bpk_read(p, 0, out + no, 64 - no); assert(n >= 0); no += n;
        n = bpk_read(p, 1, err + ne, 64 - ne); assert(n >= 0); ne += n;
        assert(now() < until); usleep(1000);
    } while (bpk_poll(p) || !bpk_eof(p, 0) || !bpk_eof(p, 1));
    assert(no == 5 && !memcmp(out, "A\0B\xffZ", 5));
    assert(ne == 10 && !memcmp(err, "error-only", 10)); assert(bpk_exit(p) == 23);
    assert(!strcmp(getenv("BPK_SLICE_ENV"), "parent-value")); bpk_delete(p);
    p = spawn(argv[0], "tree", 1); no = 0; until = now() + 15;
    while (!memchr(out, '\n', (size_t)no)) {
        int64_t n = bpk_read(p, 0, out + no, 63 - no); assert(n >= 0); no += n;
        assert(no < 63 && now() < until); usleep(1000);
    }
    out[no] = 0; pid_t descendant = (pid_t)strtol((char *)out, NULL, 10); assert(descendant > 1);
    assert(!dead(descendant)); assert(bpk_terminate_tree(p));
    while (bpk_poll(p)) { assert(now() < until); usleep(1000); }
    assert(bpk_terminate_tree(p)); bpk_delete(p);
    while (!dead(descendant)) { assert(now() < until); usleep(1000); }
    puts("ARM64 emulated spawn/binary pipes/env/exit/tree teardown assertions passed"); return 0;
}
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute(command, *, cwd=None, env=None, timeout=180, records=None):
    command = list(map(str, command))
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=timeout)
    record = dict(command=command, exitCode=result.returncode,
                  stdout=result.stdout, stderr=result.stderr)
    if records is not None:
        records.append(record)
    if result.returncode:
        raise RuntimeError(json.dumps(record))
    return record


def snapshot(work):
    paths = [ROOT / 'libraries/processkit/manifest.json']
    paths += sorted((ROOT / 'libraries/processkit/modules').rglob('*.bui'))
    paths += sorted((ROOT / 'libraries/processkit/native').rglob('*'))
    paths += [ROOT / 'tests/processkit_test.bui', ROOT / 'tests/processkit_native_test.py',
              ROOT / 'tests/run_processkit_tests.py', ROOT / 'tools/build_processkit.py',
              ROOT / 'tests/test_classic_processkit_slices.py',
              Path(__file__).resolve()]
    hashes = {}
    for path in paths:
        if path.is_file():
            relative = path.relative_to(ROOT)
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            hashes[str(relative)] = sha(destination)
    return hashes


def require_current(hashes, root=ROOT):
    for name, digest in hashes.items():
        path = root / name
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError('Source changed; rebuild before integration: ' + name)


def verify_arch(path, target):
    data = path.read_bytes()
    if target.endswith('linux'):
        valid = (len(data) >= 20 and data[:6] == b'\x7fELF\x02\x01' and
                 struct.unpack_from('<H', data, 18)[0] == 183)
    else:
        if len(data) < 64:
            raise RuntimeError('Truncated PE header: ' + str(path))
        offset = struct.unpack_from('<I', data, 60)[0]
        valid = (offset >= 64 and offset + 6 <= len(data) and
                 data[:2] == b'MZ' and data[offset:offset+4] == b'PE\0\0' and
                 struct.unpack_from('<H', data, offset + 4)[0] == 0xAA64)
    if not valid:
        raise RuntimeError('Wrong architecture: ' + str(path))


def docker(image, work, command, records):
    name = 'processkit-slices-' + uuid.uuid4().hex
    try:
        return execute(['docker', 'run', '--rm', '--name', name, '--network', 'none',
                        '--user', f'{os.getuid()}:{os.getgid()}',
                        '-v', f'{work}:/work', '-w', '/work', '--entrypoint', '/usr/bin/env',
                        image, *command], records=records, timeout=180)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


def linux_build(work, image, records):
    docker(image, work, ['aarch64-linux-gnu-gcc', '--version'], records)
    docker(image, work, ['qemu-aarch64', '--version'], records)
    docker(image, work, ['dpkg-query', '-W', 'libc6:arm64', 'libc6-dev-arm64-cross',
                         'gcc-aarch64-linux-gnu', 'qemu-user'], records)
    flags = ['-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
    docker(image, work, ['aarch64-linux-gnu-gcc', *flags, '-fPIC', '-shared',
                         'libraries/processkit/native/bridge.c',
                         '-Wl,-soname,libbuoy_processkit.so', '-o', 'libbuoy_processkit.so'], records)
    docker(image, work, ['aarch64-linux-gnu-gcc', *flags, 'harness.c', '-L/work',
                         '-lbuoy_processkit', '-Wl,-rpath,/work', '-o', 'arm64-harness'], records)
    docker(image, work, ['qemu-aarch64', '-L', '/', '/work/arm64-harness'], records)
    return work / 'libbuoy_processkit.so', None


def native_regression(work, records):
    library = work / 'host/libbuoy_processkit.so'
    library.parent.mkdir()
    execute(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
             '-fPIC', '-shared', work / 'libraries/processkit/native/bridge.c',
             '-o', library], records=records)
    execute([sys.executable, work / 'tests/processkit_native_test.py', library],
            records=records, timeout=60)


def windows_build(work, zig, records):
    # Reuse an existing tool read-only; all generated caches live in the temp build.
    env = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(work / 'zig-global'),
               ZIG_LOCAL_CACHE_DIR=str(work / 'zig-local'))
    execute([zig, 'version'], records=records)
    dll, lib = work / 'buoy_processkit.dll', work / 'buoy_processkit.lib'
    execute([zig, 'cc', '-target', 'aarch64-windows-gnu', '-std=c11', '-O2',
             '-Wall', '-Wextra', '-Werror', '-shared',
             work / 'libraries/processkit/native/bridge.c', '-o', dll,
             '-Wl,--out-implib,' + str(lib)], cwd=work, env=env, records=records)
    return dll, lib


def buoy_consumer(work, binary, library, target, image, records):
    package = work / 'package'
    shutil.copytree(work / 'libraries/processkit', package)
    triple = TRIPLES[target]
    runtime = package / 'runtime' / triple
    runtime.mkdir(parents=True)
    shutil.copy2(binary, runtime / binary.name)
    if library:
        link = package / 'link' / triple
        link.mkdir(parents=True)
        shutil.copy2(library, link / library.name)
    (work / 'packages').mkdir()
    execute(['/usr/bin/buoy', 'pkg', 'seal', package, '-o',
             work / 'packages/processkit-0.1.0.buipkg'], records=records)
    shutil.copy2(work / 'tests/processkit_test.bui', work / 'consumer.bui')
    for check_target in ['x86_64-linux', 'arm64-linux', 'x86_64-darwin',
                         'arm64-darwin', 'x86_64-windows', 'arm64-windows']:
        record = execute(['buoy', 'consumer.bui', '--check', '--diagnostic-format',
                          'json', '--target', check_target], cwd=work, records=records)
        if json.loads(record['stdout']).get('errors') != []:
            raise RuntimeError('Nonempty compiler diagnostics: ' + record['stdout'])
    consumer = work / ('consumer.exe' if library else 'consumer')
    execute(['buoy', 'consumer.bui', '--target', target, '-o', consumer], cwd=work, records=records)
    verify_arch(consumer, target)
    if target.endswith('linux'):
        spec = importlib.util.spec_from_file_location('snapshot_fixture', work / 'tests/run_processkit_tests.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (work / 'fixture.py').write_text(module.FIXTURE)
        # Copy only the already installed foreign Buoy runtime, never modify it.
        shutil.copytree(Path('/usr/lib/buoy/runtime') / triple, work / 'buoy-runtime')
        docker(image, work, ['BUOY_PROCESSKIT_PARENT=parent-baseline',
                             'BUOY_PROCESSKIT_INHERITED=inherited-baseline',
                             'qemu-aarch64', '-L', '/', '-E',
                             'LD_LIBRARY_PATH=/work/buoy-runtime:/work',
                             '/work/consumer', '/usr/bin/python3', '/work/fixture.py'], records)
    return consumer


def verify_receipt(path):
    record = json.loads(path.read_text())
    if record.get('status') != 'verified':
        raise RuntimeError('Receipt is not verified')
    target = record.get('target')
    if record.get('schema') != 1 or target not in TRIPLES or record.get('triple') != TRIPLES[target]:
        raise RuntimeError('Invalid receipt schema or target')
    required_sources = {'libraries/processkit/native/bridge.c',
                        'libraries/processkit/manifest.json',
                        'libraries/processkit/modules/ProcessKit.bui'}
    if not required_sources.issubset(record.get('sources', {})):
        raise RuntimeError('Receipt omits required package sources')
    if record.get('nativeSourceSha256') != record['sources']['libraries/processkit/native/bridge.c']:
        raise RuntimeError('Native source receipt disagrees with snapshot')
    require_current(record['sources'])
    filename = 'buoy_processkit.dll' if target.endswith('windows') else 'libbuoy_processkit.so'
    runtime = f'runtime/{TRIPLES[target]}/{filename}'
    required_artifacts = {runtime, runtime + '.source-sha256'}
    if target.endswith('windows'):
        required_artifacts.add(f'link/{TRIPLES[target]}/buoy_processkit.lib')
    if not required_artifacts.issubset(record.get('artifacts', {})):
        raise RuntimeError('Receipt omits required native artifacts')
    for relative, digest in record['artifacts'].items():
        artifact = path.parent / relative
        if not artifact.is_file() or sha(artifact) != digest:
            raise RuntimeError('Artifact changed: ' + str(artifact))
    if (path.parent / (runtime + '.source-sha256')).read_text().strip() != record['nativeSourceSha256']:
        raise RuntimeError('Runtime source receipt disagrees with snapshot')
    verify_arch(path.parent / runtime, target)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=TRIPLES, action='append')
    parser.add_argument('--image', default=IMAGE, help='Existing local image; never pulled/built')
    parser.add_argument('--zig', type=Path, default=ZIG, help='Existing Zig executable; never downloaded')
    parser.add_argument('--verify-receipt', type=Path)
    args = parser.parse_args()
    if args.verify_receipt:
        verify_receipt(args.verify_receipt)
        print('Source and artifact hashes match; receipt is eligible for parent integration.')
        return
    OUTPUT.mkdir(parents=True, exist_ok=True)
    run = OUTPUT / uuid.uuid4().hex
    run.mkdir()
    failed = False
    for target in args.target or TRIPLES:
        destination = run / target
        destination.mkdir()
        record = dict(schema=1, target=target, triple=TRIPLES[target], status='blocked',
                      execution='not executed', commands=[], artifacts={})
        try:
            with tempfile.TemporaryDirectory(prefix='classic-processkit-') as directory:
                work = Path(directory)
                record['sources'] = snapshot(work)
                for relative in record['sources']:
                    saved = destination / 'snapshot' / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(work / relative, saved)
                (work / 'harness.c').write_text(HARNESS)
                (destination / 'harness.c').write_text(HARNESS)
                record['harnessSha256'] = sha(destination / 'harness.c')
                source = work / 'libraries/processkit/native/bridge.c'
                record['nativeSourceSha256'] = sha(source)
                record['buoyVersion'] = execute(['buoy', '--version'], records=record['commands'])['stdout'].strip()
                record['buoyCompilerSha256'] = sha('/usr/bin/buoy')
                if target.endswith('linux'):
                    native_regression(work, record['commands'])
                    record['image'] = execute(['docker', 'image', 'inspect', args.image,
                                              '--format', '{{.Id}}'], records=record['commands'])['stdout'].strip()
                    binary, library = linux_build(work, record['image'], record['commands'])
                    record['execution'] = 'ARM64 bridge, driver and child fixture under QEMU user emulation on x86_64; not native ARM hardware'
                else:
                    if not args.zig.is_file():
                        raise RuntimeError('Need an existing ARM64 Windows C compiler, headers, CRT and linker (e.g. Zig); no tool downloaded')
                    record['zigExecutableSha256'] = sha(args.zig)
                    binary, library = windows_build(work, args.zig, record['commands'])
                    record['execution'] = 'cross-compiled and cross-linked only; no Windows ARM64 execution'
                verify_arch(binary, target)
                exports = execute(['llvm-readobj-19', '--dyn-symbols' if library is None else '--coff-exports',
                                   binary], records=record['commands'])['stdout']
                expected = sorted(set(re.findall(r'\bAPI\s+[^\n{]*?\b(bpk_\w+)\s*\(', source.read_text())))
                missing = [name for name in expected if not re.search(r'\b' + name + r'\b', exports)]
                if missing:
                    raise RuntimeError('Missing native exports: ' + repr(missing))
                record['exports'] = expected
                record['dependencies'] = execute(['llvm-readobj-19', '--coff-imports' if library else '--needed-libs',
                                                  binary], records=record['commands'])['stdout']
                if library is None:
                    record['elfVersions'] = execute(['llvm-readelf-19', '--version-info', binary],
                                                     records=record['commands'])['stdout']
                consumer = buoy_consumer(work, binary, library, target, record.get('image'), record['commands'])
                artifacts = [(binary, 'runtime'), (library, 'link'), (consumer, 'proof')]
                if library is None:
                    artifacts.append((work / 'arm64-harness', 'proof'))
                for artifact, folder in artifacts:
                    if artifact is None:
                        continue
                    relative = Path(folder) / TRIPLES[target] / artifact.name
                    exported = destination / relative
                    exported.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(artifact, exported)
                    record['artifacts'][str(relative)] = sha(exported)
                    if folder == 'runtime':
                        receipt = exported.with_name(exported.name + '.source-sha256')
                        receipt.write_text(record['nativeSourceSha256'] + '\n')
                        record['artifacts'][str(receipt.relative_to(destination))] = sha(receipt)
                require_current(record['sources'])
                record['status'] = 'verified'
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            record['error'] = str(error)
            failed = True
        receipt = destination / 'receipt.json'
        receipt.write_text(json.dumps(record, indent=2) + '\n')
        print(f'{target}: {record["status"]}: {receipt}', flush=True)
        if record.get('error'):
            print(record['error'], file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
