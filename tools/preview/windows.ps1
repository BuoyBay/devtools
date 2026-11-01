# Executed on the existing VM's interactive console, never in the SSH session.
$ErrorActionPreference = 'Stop'
$directory = $PSScriptRoot
Set-Location -LiteralPath $directory
$process = $null
$owned = $null
$report = @{ status = 'failed' }
try {
    if (Test-Path -LiteralPath "$directory/cancel") { throw 'Job cancelled before launch' }
    $request = Get-Content -Raw -Encoding UTF8 -LiteralPath "$directory/request.json" | ConvertFrom-Json
    $deadline = [DateTime]::UtcNow.AddSeconds($request.timeout)
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public class PreviewCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr window, out RECT rect);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr window, ref POINT point);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr window);
}
'@
    Add-Type @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
public sealed class PreviewOwnedProcess : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct SECURITY_ATTRIBUTES { public int Length; public IntPtr Descriptor; public int Inherit; }
    [StructLayout(LayoutKind.Sequential)] struct STARTUPINFO {
        public int Size; public IntPtr Reserved, Desktop, Title; public int X,Y,Width,Height,XChars,YChars,Fill,Flags;
        public short Show, ReservedSize; public IntPtr ReservedData, Input, Output, Error;
    }
    [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION { public IntPtr Process, Thread; public int Pid, Tid; }
    [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMITS {
        public long ProcessTime, JobTime; public uint Flags; public UIntPtr MinWorking, MaxWorking;
        public uint ActiveProcesses; public UIntPtr Affinity; public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS { public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMITS { public BASIC_LIMITS Basic; public IO_COUNTERS Io; public UIntPtr ProcessMemory,JobMemory,PeakProcess,PeakJob; }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING { public long User,Kernel,PeriodUser,PeriodKernel; public uint Faults,Total,Active,Terminated; }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int info, ref EXTENDED_LIMITS value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job, int info, out ACCOUNTING value, uint size, IntPtr returned);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CreateProcess(string app, string command, IntPtr processAttrs, IntPtr threadAttrs, bool inherit, uint flags, IntPtr env, string cwd, ref STARTUPINFO startup, out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateFile(string path, uint access, uint share, ref SECURITY_ATTRIBUTES attributes, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    IntPtr job;
    public Process Process { get; private set; }
    public bool CleanupComplete { get; private set; }
    static IntPtr Open(string path, uint access, uint creation) {
        var attributes = new SECURITY_ATTRIBUTES { Length=Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)), Inherit=1 };
        var handle = CreateFile(path,access,7,ref attributes,creation,0x80,IntPtr.Zero);
        if(handle == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return handle;
    }
    public static PreviewOwnedProcess Start(string app, string cwd) {
        var owned = new PreviewOwnedProcess();
        PROCESS_INFORMATION native = new PROCESS_INFORMATION();
        IntPtr input=IntPtr.Zero, output=IntPtr.Zero, error=IntPtr.Zero;
        bool resumed=false;
        try {
            owned.job=CreateJobObject(IntPtr.Zero,null);
            if(owned.job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            var limits=new EXTENDED_LIMITS(); limits.Basic.Flags=0x2000; // KILL_ON_JOB_CLOSE
            if(!SetInformationJobObject(owned.job,9,ref limits,(uint)Marshal.SizeOf(typeof(EXTENDED_LIMITS)))) throw new Win32Exception(Marshal.GetLastWin32Error());
            input=Open("NUL",0x80000000,3);
            output=Open(System.IO.Path.Combine(cwd,"runtime.log"),0x40000000,2);
            error=Open(System.IO.Path.Combine(cwd,"runtime-error.log"),0x40000000,2);
            var startup=new STARTUPINFO { Size=Marshal.SizeOf(typeof(STARTUPINFO)), Flags=0x100, Input=input,Output=output,Error=error };
            if(!CreateProcess(app,null,IntPtr.Zero,IntPtr.Zero,true,0x08000004,IntPtr.Zero,cwd,ref startup,out native)) throw new Win32Exception(Marshal.GetLastWin32Error());
            // Assignment happens before any user code can spawn children.
            if(!AssignProcessToJobObject(owned.job,native.Process)) throw new Win32Exception(Marshal.GetLastWin32Error());
            owned.Process=System.Diagnostics.Process.GetProcessById(native.Pid);
            if(ResumeThread(native.Thread)==0xffffffff) throw new Win32Exception(Marshal.GetLastWin32Error());
            resumed=true;
            return owned;
        } finally {
            if(!resumed) { if(native.Process!=IntPtr.Zero) TerminateProcess(native.Process,1); owned.Dispose(); }
            foreach(var handle in new [] {native.Process,native.Thread,input,output,error}) if(handle!=IntPtr.Zero) CloseHandle(handle);
        }
    }
    public void Dispose() {
        if(job!=IntPtr.Zero) {
            CleanupComplete=false;
            TerminateJobObject(job,1);
            var deadline=DateTime.UtcNow.AddSeconds(5);
            do {
                ACCOUNTING state;
                if(!QueryInformationJobObject(job,1,out state,(uint)Marshal.SizeOf(typeof(ACCOUNTING)),IntPtr.Zero)) break;
                if(state.Active==0) { CleanupComplete=true; break; }
                System.Threading.Thread.Sleep(10);
            } while(DateTime.UtcNow<deadline);
            CloseHandle(job); job=IntPtr.Zero;
        }
        if(Process!=null) { Process.Dispose(); Process=null; }
    }
}
'@
    [void][PreviewCapture]::SetProcessDPIAware()
    $owned = [PreviewOwnedProcess]::Start("$directory/bin/preview.exe", $directory)
    $process = $owned.Process
    function Test-OutputBudget {
        foreach ($name in @('runtime.log', 'runtime-error.log')) {
            $file = Get-Item -LiteralPath "$directory/$name" -ErrorAction SilentlyContinue
            if ($null -ne $file -and $file.Length -gt 1048576) { throw 'Runtime output exceeded the 1 MiB per-stream budget' }
        }
    }
    $ready = $null
    $handle = [IntPtr]::Zero
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath "$directory/cancel") { throw 'Job cancelled' }
        Test-OutputBudget
        $process.Refresh()
        if ($process.HasExited) { throw "Preview exited before readiness ($($process.ExitCode))" }
        if (Test-Path -LiteralPath "$directory/_buoy_preview_ready.json") {
            try { $ready = Get-Content -Raw -Encoding UTF8 -LiteralPath "$directory/_buoy_preview_ready.json" | ConvertFrom-Json } catch { }
        }
        $handle = $process.MainWindowHandle
        if ($null -ne $ready -and $handle -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 50
    }
    if ($null -eq $ready -or $handle -eq [IntPtr]::Zero) { throw 'No ready native preview window' }
    [void][PreviewCapture]::SetForegroundWindow($handle)
    $previous = ''
    $identical = 0
    $stable = $false
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-Path -LiteralPath "$directory/cancel") { throw 'Job cancelled' }
            Test-OutputBudget
            $process.Refresh()
            if ($process.HasExited) { throw 'Preview exited during capture' }
            if ([PreviewCapture]::GetForegroundWindow() -ne $handle) { throw 'Preview lost foreground; refusing an obscured screenshot' }
            $rect = New-Object PreviewCapture+RECT
            $point = New-Object PreviewCapture+POINT
            if (-not [PreviewCapture]::GetClientRect($handle, [ref]$rect)) { throw 'GetClientRect failed' }
            if (-not [PreviewCapture]::ClientToScreen($handle, [ref]$point)) { throw 'ClientToScreen failed' }
            $width = $rect.Right - $rect.Left
            $height = $rect.Bottom - $rect.Top
            $screen = [System.Windows.Forms.Screen]::FromHandle($handle).Bounds
            if ($width -le 0 -or $height -le 0 -or $point.X -lt $screen.Left -or $point.Y -lt $screen.Top -or
                ($point.X + $width) -gt $screen.Right -or ($point.Y + $height) -gt $screen.Bottom) {
                throw 'Client area is empty or outside the screen; use a smaller layout'
            }
            $bitmap = New-Object System.Drawing.Bitmap($width, $height)
            try {
                $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
                try { $graphics.CopyFromScreen($point.X, $point.Y, 0, 0, $bitmap.Size) }
                finally { $graphics.Dispose() }
                if ([PreviewCapture]::GetForegroundWindow() -ne $handle) { throw 'Preview lost foreground during capture' }
                $memory = New-Object System.IO.MemoryStream
                try {
                    $bitmap.Save($memory, [System.Drawing.Imaging.ImageFormat]::Png)
                    $bytes = $memory.ToArray()
                } finally { $memory.Dispose() }
            } finally { $bitmap.Dispose() }
            $digest = [Convert]::ToBase64String($sha.ComputeHash($bytes))
            if ($digest -eq $previous) { $identical++ } else { $identical = 0 }
            if ($identical -ge 2) { $stable = $true; break }
            $previous = $digest
            Start-Sleep -Milliseconds 150
        }
    } finally { $sha.Dispose() }
    if (-not $stable) { throw 'Native pixels did not stabilize; stop animations in the fixture' }
    [System.IO.File]::WriteAllBytes("$directory/preview.png", $bytes)
    $personalize = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' -ErrorAction SilentlyContinue
    $report = @{ status = 'passed'; result = @{
        layout = $ready
        capture = @{ scope = 'client'; backend = 'win32-screen'; stableFrames = 3; x = $point.X; y = $point.Y }
        environment = @{ os = [Environment]::OSVersion.VersionString; culture = [Globalization.CultureInfo]::CurrentCulture.Name
            dpi = [PreviewCapture]::GetDpiForWindow($handle); appsUseLightTheme = $personalize.AppsUseLightTheme
            systemFont = [System.Drawing.SystemFonts]::MessageBoxFont.ToString(); screenWidth = $screen.Width; screenHeight = $screen.Height }
    } }
} catch {
    $report = @{ status = 'failed'; error = $_.Exception.Message }
} finally {
    # Closing this job kills all descendants even if the root already exited.
    if ($null -ne $owned) {
        $owned.Dispose()
        if (-not $owned.CleanupComplete) {
            $report.cleanupErrors = @('Owned process job did not terminate within 5s')
            if ($report.status -eq 'passed') { $report.status = 'failed'; $report.error = 'Windows preview job cleanup did not complete' }
        }
    }
    foreach ($name in @('runtime.log', 'runtime-error.log')) {
        if (-not (Test-Path -LiteralPath "$directory/$name")) { [IO.File]::WriteAllText("$directory/$name", '') }
    }
    foreach ($name in @('runtime.log', 'runtime-error.log')) {
        $file = Get-Item -LiteralPath "$directory/$name"
        if ($file.Length -gt 1048576) {
            $stream = [IO.File]::OpenRead($file.FullName)
            try {
                $buffer = New-Object byte[] 1048576
                $count = 0
                while ($count -lt $buffer.Length) {
                    $read = $stream.Read($buffer, $count, $buffer.Length - $count)
                    if ($read -eq 0) { break }
                    $count += $read
                }
            } finally { $stream.Dispose() }
            $kept = New-Object byte[] $count
            [Array]::Copy($buffer, $kept, $count)
            [IO.File]::WriteAllBytes($file.FullName, $kept)
            $report.outputTruncated = $true
        }
    }
    $report | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 -LiteralPath "$directory/result.tmp"
    Move-Item -Force -LiteralPath "$directory/result.tmp" -Destination "$directory/result.json"
}
