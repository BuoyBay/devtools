#!/usr/bin/env python3
"""Studio-owned DAP bridge. JSONL on stdin/stdout; native DAP adapter as child.

No project scripts run on open. A launch or attach request is explicit. Paths and
arguments are data, never shell text. stderr is diagnostic output only.
"""
import argparse
from collections import deque
import json
import os
import plistlib
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

LIMIT = 4 * 1024 * 1024


def adapter_path():
    configured = os.environ.get('BUOY_STUDIO_DEBUG_ADAPTER')
    if configured:
        return configured
    for candidate in ('/Applications/Buoy/buoy-dap', '/usr/share/buoy/buoy-dap'):
        if Path(candidate).is_file():
            return candidate
    if sys.platform == 'darwin' and Path('/usr/bin/xcrun').is_file():
        found = subprocess.run(['/usr/bin/xcrun', '--find', 'lldb-dap'], capture_output=True, text=True, timeout=10)
        candidate = found.stdout.strip()
        if found.returncode == 0 and Path(candidate).is_file():
            return candidate
    # On macOS respect the selected Xcode/Developer directory before falling
    # back to a separate CLT/Homebrew installation with different policies.
    for candidate in ('/opt/homebrew/bin/lldb-dap', '/opt/homebrew/opt/llvm/bin/lldb-dap',
                      '/Library/Developer/CommandLineTools/usr/bin/lldb-dap',
                      '/usr/bin/lldb-dap', '/usr/bin/lldb-dap-19'):
        if Path(candidate).is_file():
            return candidate
    # Ignore relative PATH entries and project-local executables. Opening a
    # project must not turn its lldb-dap lookalike into trusted tooling.
    project = Path.cwd().resolve()
    for name in ('buoy-dap', 'lldb-dap', 'lldb-dap-19'):
        for directory in os.get_exec_path():
            folder = Path(directory)
            if not folder.is_absolute():
                continue
            candidate = folder / name
            resolved = candidate.resolve()
            if resolved.is_relative_to(project):
                continue
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def dap_error_message(value):
    """Keep adapter-specific structured errors visible (Apple LLDB uses body.error)."""
    parts = []
    if value.get('message'):
        parts.append(str(value['message']))
    body = value.get('body')
    error = body.get('error') if isinstance(body, dict) else None
    if isinstance(error, dict):
        detail = error.get('format') or error.get('message')
        if detail:
            detail = str(detail)
            variables = error.get('variables')
            if isinstance(variables, dict):
                for name, text in variables.items():
                    detail = detail.replace('{' + str(name) + '}', str(text))
            if detail not in parts:
                parts.append(detail)
        if not parts and error.get('id') is not None:
            parts.append('adapter error ' + str(error['id']))
    elif error:
        parts.append(str(error))
    return ': '.join(parts) or 'debug adapter rejected request'


def mac_debugserver_descendants(adapter_pid):
    """Retain identities of this adapter's debugservers before it can orphan them.

    Apple debugserver starts a separate session, so terminating the adapter's
    process group alone cannot clean up a launch blocked in OS authorization.
    External attach targets are never selected by this descendant-only query.
    """
    if sys.platform != 'darwin':
        return {}
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,lstart=,comm='],
                            capture_output=True, text=True, timeout=3,
                            env=dict(os.environ, LC_ALL='C'))
    if result.returncode:
        raise RuntimeError('Unable to inspect debugger descendants')
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 7)
        if len(fields) != 8:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        rows[pid] = (parent, ' '.join(fields[2:7]), fields[7])
    descendants = {adapter_pid}
    for _ in range(len(rows)):
        added = {pid for pid, row in rows.items() if row[0] in descendants} - descendants
        if not added:
            break
        descendants.update(added)
    return {pid: (row[1], row[2]) for pid, row in rows.items()
            if pid != adapter_pid and pid in descendants and Path(row[2]).name == 'debugserver'}


def close_mac_debugservers(identities):
    for pid, identity in identities.items():
        # Verify start time and executable, not merely a potentially reused PID.
        checked = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=,comm='],
                                 capture_output=True, text=True, timeout=3,
                                 env=dict(os.environ, LC_ALL='C'))
        fields = checked.stdout.strip().split(None, 5)
        if checked.returncode or len(fields) != 6:
            continue
        if (' '.join(fields[:5]), fields[5]) == identity:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class AdapterWriter:
    # One raw-write worker owns adapter stdin. The event loop only enqueues.
    # The byte budget includes the in-flight packet; disconnect has a small
    # reserved control budget and drops unsent work without truncating a frame.
    def __init__(self, stream, inbox, limit=1024 * 1024):
        self.stream = stream
        self.inbox = inbox
        self.limit = limit
        self.condition = threading.Condition()
        self.packets = deque()
        self.bytes = 0
        self.closed = False
        self.failure = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def offer(self, data, disconnect=False):
        with self.condition:
            if self.closed or self.failure:
                raise ValueError('Debug adapter writer is closed')
            if disconnect:
                if len(data) > 4096:
                    raise ValueError('Disconnect packet exceeds control budget')
                self.bytes -= sum(len(packet) for packet in self.packets)
                self.packets.clear()
            budget = self.limit + (4096 if disconnect else 0)
            if len(data) > self.limit or self.bytes + len(data) > budget:
                raise ValueError('Debug adapter write queue is full')
            self.packets.append(data)
            self.bytes += len(data)
            self.condition.notify()

    def run(self):
        try:
            while True:
                with self.condition:
                    while not self.closed and not self.packets:
                        self.condition.wait()
                    if self.closed:
                        return
                    packet = self.packets.popleft()
                offset = 0
                while offset < len(packet):
                    count = os.write(self.stream.fileno(), packet[offset:])
                    if count <= 0:
                        raise OSError('Debug adapter stdin closed')
                    offset += count
                with self.condition:
                    self.bytes -= len(packet)
        except (OSError, ValueError) as exc:
            with self.condition:
                self.failure = str(exc)
            try:
                self.inbox.put_nowait(('error', 'Debug adapter write failed: ' + str(exc)))
            except queue.Full:
                pass

    def close(self):
        # Never close/flush a blocked buffered pipe on the event-loop thread.
        # Child termination closes the read end and releases any raw os.write.
        with self.condition:
            self.closed = True
            self.packets.clear()
            self.condition.notify_all()


class Bridge:
    def __init__(self, adapter, output=None, startup_timeout=30, stop_timeout=4, shutdown_timeout=2):
        self.adapter = adapter
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.shutdown_timeout = shutdown_timeout
        self.stopping = False
        self.writer = None
        self.output = output or sys.stdout
        self.sequence = 0
        self.pending = {}
        self.inbox = queue.Queue(maxsize=128)
        self.child = None
        self.thread_id = 0
        self.frame_id = 0
        self.stop_generation = 0
        self.inspect_generation = 0
        self.stopped = False
        self.target_failed = False
        self.config = None
        self.configuring = 0
        self.ready_for_config = False
        self.attaching = False
        self.running = True
        self.last_action = time.monotonic()
        self.start_deadline = None
        self.stop_deadline = None

    def emit(self, kind, **payload):
        self.output.write(json.dumps(dict(type=kind, **payload), ensure_ascii=False) + '\n')
        self.output.flush()

    def send_packet(self, packet, disconnect=False):
        data = json.dumps(packet).encode()
        if self.writer is None:
            raise ValueError('Debug adapter transport has not started')
        self.writer.offer(b'Content-Length: %d\r\n\r\n' % len(data) + data, disconnect=disconnect)

    def request(self, command, arguments=None, tag=None):
        if self.stopping and command != 'disconnect':
            return
        self.sequence += 1
        packet = dict(seq=self.sequence, type='request', command=command, arguments=arguments or {})
        self.send_packet(packet, disconnect=command == 'disconnect')
        self.pending[self.sequence] = (command, tag, time.monotonic())

    def begin_stop(self):
        if self.stopping:
            return
        # Latch before disconnect is queued: late initialize/config responses
        # must never start a target after the user has asked to stop.
        self.stopping = True
        self.start_deadline = None
        self.stop_deadline = time.monotonic() + self.stop_timeout
        self.stopped = False
        self.stop_generation += 1
        self.inspect_generation += 1
        self.ready_for_config = False
        self.configuring = 0
        self.pending.clear()
        self.emit('state', state='stopping')
        self.request('disconnect', dict(terminateDebuggee=not self.attaching))

    def read_dap(self):
        try:
            stream = self.child.stdout
            while True:
                headers = {}
                total = 0
                while True:
                    line = stream.readline(8193)
                    total += len(line)
                    if not line:
                        if total:
                            raise ValueError('Truncated DAP header')
                        self.inbox.put(('eof', None))
                        return
                    if len(line) > 8192 or total > 16384:
                        raise ValueError('DAP header exceeds limit')
                    if line in (b'\r\n', b'\n'):
                        break
                    name, value = line.decode('ascii').split(':', 1)
                    headers[name.lower()] = value.strip()
                count = int(headers.get('content-length', '-1'))
                if not 0 < count <= LIMIT:
                    raise ValueError('Invalid DAP content length')
                data = bytearray()
                while len(data) < count:
                    chunk = stream.read(count-len(data))
                    if not chunk:
                        raise ValueError('Truncated DAP body')
                    data.extend(chunk)
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError('DAP envelope must be an object')
                self.inbox.put(('dap', value))
        except Exception as exc:
            self.inbox.put(('error', str(exc)))

    def read_stderr(self):
        while True:
            chunk = self.child.stderr.read1(8192)
            if not chunk:
                return
            self.inbox.put(('stderr', chunk.decode('utf-8', 'replace')))

    def read_commands(self):
        # os.read avoids leaving a buffered sys.stdin lock held by a daemon
        # reader when an explicit Disconnect ends the bridge before stdin EOF.
        buffered = bytearray()
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 8192)
                if not data:
                    self.inbox.put(('quit', None))
                    return
                buffered.extend(data)
                while b'\n' in buffered:
                    line, _, tail = buffered.partition(b'\n')
                    buffered = bytearray(tail)
                    if len(line) > 65536:
                        raise ValueError('Debugger command exceeds limit')
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('Debugger command must be an object')
                    self.inbox.put(('command', value))
                if len(buffered) > 65536:
                    raise ValueError('Debugger command exceeds limit')
        except Exception as exc:
            self.inbox.put(('error', str(exc)))

    def launch(self, value):
        if self.config is not None:
            raise ValueError('One debug target per session; stop before launching again')
        cwd = str(Path(value.get('cwd') or os.getcwd()).resolve())
        if not Path(cwd).is_dir():
            raise ValueError('Project working directory does not exist')
        self.attaching = value.get('action') == 'attach'
        if self.attaching:
            pid = value.get('pid')
            if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
                raise ValueError('Attach requires a positive process id')
            target = dict(pid=pid, cwd=cwd, stopOnEntry=True)
        else:
            binary = Path(value.get('program', '')).expanduser().resolve()
            if binary.suffix == '.app' and binary.is_dir():
                with (binary / 'Contents/Info.plist').open('rb') as info:
                    executable = plistlib.load(info).get('CFBundleExecutable')
                if not isinstance(executable, str) or not executable or Path(executable).name != executable:
                    raise ValueError('Application bundle has an invalid executable name')
                binary = binary / 'Contents/MacOS' / executable
            if not binary.is_file():
                raise ValueError('Choose an existing executable built with buoy -g')
            arguments = value.get('args', [])
            if not isinstance(arguments, list) or any(not isinstance(a, str) or '\x00' in a for a in arguments):
                raise ValueError('Program arguments must be a JSON string array')
            target = dict(program=str(binary), cwd=cwd, args=arguments, stopOnEntry=True,
                          disableASLR=False, runInTerminal=False)
        points = value.get('breakpoints', [])
        if not isinstance(points, list):
            raise ValueError('Breakpoints must be an array')
        grouped = {}
        for point in points:
            # DWARF records the compiler's lexical source path. On macOS /var
            # and /tmp are symlink aliases; resolving them to /private/...
            # turns valid source breakpoints into unresolved locations.
            source = Path(os.path.abspath(os.path.expanduser(point['path'])))
            line = point['line']
            if not source.is_file() or not isinstance(line, int) or isinstance(line, bool) or line < 1:
                raise ValueError('Breakpoint requires an existing source file and positive line')
            grouped.setdefault(str(source), []).append(dict(line=line))
        self.config = dict(target=target, points=grouped)
        self.start_deadline = time.monotonic() + self.startup_timeout
        self.emit('state', state='initializing')
        self.request('initialize', dict(clientID='buoy-studio', clientName='Buoy Studio',
                     adapterID='lldb', pathFormat='path', linesStartAt1=True, columnsStartAt1=True,
                     supportsRunInTerminalRequest=False, supportsVariableType=True))

    def configure(self):
        if self.stopping or not self.config or not self.ready_for_config:
            return
        self.ready_for_config = False
        self.configuring = len(self.config['points'])
        for path, points in self.config['points'].items():
            self.request('setBreakpoints', dict(source=dict(path=path), breakpoints=points), path)
        if self.configuring == 0:
            self.request('configurationDone')

    def command(self, value):
        action = value.get('action')
        if action == 'stop':
            self.begin_stop()
            return
        if self.stopping:
            raise ValueError('Debugger is stopping; further commands are refused')
        if action in ('launch', 'attach'):
            self.launch(value)
        elif action in ('continue', 'pause', 'next', 'stepIn', 'stepOut'):
            if not self.thread_id:
                raise ValueError('Wait for a target thread before using debugger controls')
            self.request(action, dict(threadId=self.thread_id))
        elif action == 'frame':
            frame = value.get('frameId')
            if not isinstance(frame, int) or isinstance(frame, bool):
                raise ValueError('Frame id must be an integer')
            if not self.stopped:
                raise ValueError('Frames are available only while stopped')
            self.frame_id = frame
            self.inspect_generation += 1
            self.request('scopes', dict(frameId=frame), (self.stop_generation, self.inspect_generation))
        else:
            raise ValueError('Unknown debugger action')

    def dap(self, value):
        kind = value.get('type')
        if kind == 'request':
            if self.stopping:
                return
            # Never honor reverse requests such as runInTerminal/evaluate scripts.
            self.sequence += 1
            packet = dict(seq=self.sequence, type='response', request_seq=value.get('seq'),
                          command=value.get('command'), success=False,
                          message='Studio does not support reverse adapter requests')
            self.send_packet(packet)
            return
        if kind == 'event':
            event, body = value.get('event'), value.get('body') or {}
            if self.stopping and event not in ('output', 'terminated', 'exited'):
                return
            if self.target_failed and event in ('stopped', 'continued'):
                return
            if event == 'initialized':
                self.ready_for_config = True
                self.configure()
            elif event == 'stopped':
                self.stop_generation += 1
                self.stopped = True
                self.start_deadline = None
                self.thread_id = body.get('threadId', 0)
                self.emit('state', state='stopped', reason=body.get('description') or body.get('reason', ''), threadId=self.thread_id)
                if self.thread_id:
                    self.request('stackTrace', dict(threadId=self.thread_id, startFrame=0, levels=50), self.stop_generation)
                else:
                    self.request('threads', tag=self.stop_generation)
            elif event == 'continued':
                self.stopped = False
                self.stop_generation += 1
                self.emit('state', state='running')
            elif event == 'output':
                self.emit('output', text=str(body.get('output', ''))[:LIMIT], category=body.get('category', 'console'))
            elif event in ('terminated', 'exited'):
                self.stopped = False
                self.stop_generation += 1
                self.emit('state', state=event, exitCode=body.get('exitCode'))
            return
        if kind != 'response':
            return
        command, tag, _ = self.pending.pop(value.get('request_seq'), ('', None, 0))
        if self.stopping and command != 'disconnect':
            return
        body = value.get('body') or {}
        if not value.get('success', False):
            self.emit('error', message=command + ': ' + dap_error_message(value))
            if command in ('initialize', 'launch', 'attach', 'configurationDone'):
                self.target_failed = True
                self.stopped = False
                self.stop_generation += 1
                self.start_deadline = None
                self.emit('state', state='failed')
            if command == 'setBreakpoints':
                self.configuring -= 1
                if self.configuring == 0:
                    self.request('configurationDone')
            return
        if command == 'initialize':
            self.emit('capabilities', capabilities=body)
            self.request('attach' if self.attaching else 'launch', self.config['target'])
        elif command == 'setBreakpoints':
            self.emit('breakpoints', path=tag, breakpoints=body.get('breakpoints', []))
            self.configuring -= 1
            if self.configuring == 0:
                self.request('configurationDone')
        elif command == 'threads':
            if not self.stopped or tag != self.stop_generation:
                return
            threads = body.get('threads') or []
            if threads:
                self.thread_id = threads[0]['id']
                self.request('stackTrace', dict(threadId=self.thread_id, startFrame=0, levels=50), self.stop_generation)
        elif command == 'stackTrace':
            if not self.stopped or tag != self.stop_generation:
                return
            frames = body.get('stackFrames', [])
            self.emit('stack', frames=frames)
            if frames:
                self.frame_id = frames[0]['id']
                self.inspect_generation += 1
                self.request('scopes', dict(frameId=self.frame_id), (self.stop_generation, self.inspect_generation))
        elif command == 'scopes':
            if not self.stopped or tag != (self.stop_generation, self.inspect_generation):
                return
            self.emit('locals', scope='', variables=[])
            for scope in body.get('scopes', []):
                reference = scope.get('variablesReference', 0)
                if reference and not scope.get('expensive', False):
                    self.request('variables', dict(variablesReference=reference), (tag, scope.get('name', 'Locals')))
        elif command == 'variables':
            if not self.stopped or not tag or tag[0] != (self.stop_generation, self.inspect_generation):
                return
            self.emit('locals', scope=tag[1], variables=body.get('variables', []))
        elif command == 'disconnect':
            self.running = False

    def spawn_adapter(self):
        return subprocess.Popen([self.adapter], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt')

    def run(self):
        self.child = self.spawn_adapter()
        self.writer = AdapterWriter(self.child.stdin, self.inbox)
        for target in (self.read_dap, self.read_stderr, self.read_commands):
            threading.Thread(target=target, daemon=True).start()
        self.emit('ready', adapter=self.adapter)
        debugservers = {}
        try:
            while self.running:
                if self.child.poll() is None:
                    debugservers.update(mac_debugserver_descendants(self.child.pid))
                if self.writer.failure:
                    self.emit('error', message='Debug adapter write failed: ' + self.writer.failure)
                    break
                if self.stop_deadline and time.monotonic() > self.stop_deadline:
                    self.emit('error', message='Debugger did not acknowledge disconnect; closing adapter')
                    break
                if self.start_deadline and time.monotonic() > self.start_deadline:
                    self.emit('error', message='Debugger initialization timed out')
                    break
                try:
                    kind, value = self.inbox.get(timeout=.1)
                except queue.Empty:
                    if self.start_deadline and time.monotonic() > self.start_deadline:
                        self.emit('error', message='Debugger initialization timed out')
                        break
                    continue
                if kind == 'dap':
                    self.dap(value)
                elif kind == 'command':
                    try:
                        self.command(value)
                    except (ValueError, KeyError, TypeError) as exc:
                        self.emit('error', message=str(exc))
                elif kind == 'stderr':
                    self.emit('output', text=value, category='adapter stderr')
                elif kind == 'error':
                    self.emit('error', message=value)
                    break
                else:
                    break
        finally:
            # Queue a best-effort disconnect, never synchronously write it.
            try:
                if self.child.poll() is None:
                    self.begin_stop()
            except ValueError:
                pass
            try:
                # A live, unreaped child pins this PID while ownership is read.
                # Once poll() has reaped it, do not attribute any new PID tree.
                if self.child.poll() is None:
                    debugservers.update(mac_debugserver_descendants(self.child.pid))
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                self.emit('output', text='Debugger cleanup: ' + str(exc) + '\n', category='adapter stderr')
            try:
                if self.child.poll() is None:
                    self.child.wait(timeout=self.shutdown_timeout)
            except (OSError, subprocess.TimeoutExpired):
                if not self.attaching:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(self.child.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    else:
                        try:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                self.child.terminate()
                try:
                    self.child.wait(timeout=self.shutdown_timeout)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait()
            self.writer.close()
            self.writer.thread.join(timeout=1)
            try:
                close_mac_debugservers(debugservers)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.emit('output', text='Debugger cleanup: ' + str(exc) + '\n', category='adapter stderr')
            self.emit('state', state='disconnected')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adapter')
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    adapter = args.adapter or adapter_path()
    if not adapter or not Path(adapter).is_file():
        print(json.dumps(dict(type='error', message='No native debug adapter found. Install buoy-dap or lldb-dap; set BUOY_STUDIO_DEBUG_ADAPTER to its executable.')), flush=True)
        return 2
    if args.probe:
        print(json.dumps(dict(type='ready', adapter=adapter)), flush=True)
        return 0
    try:
        Bridge(adapter).run()
    except Exception as exc:
        print(json.dumps(dict(type='error', message=str(exc))), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
