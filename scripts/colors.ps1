# Researchy palette demo
# Foreground: #F6E9D9 (cream)  -> 246;233;217
# Background: #043222 (dark green) -> 4;50;34
$e = [char]27
$fg = "38;2;246;233;217"
$bg = "48;2;4;50;34"

# Enable ANSI/VT processing in classic conhost (no-op in Windows Terminal)
try {
    $sig = '[DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int h); [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out int m); [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, int m);'
    $k = Add-Type -MemberDefinition $sig -Name VT -Namespace Win32 -PassThru
    $h = $k::GetStdHandle(-11)
    $m = 0; [void]$k::GetConsoleMode($h, [ref]$m)
    [void]$k::SetConsoleMode($h, $m -bor 0x0004)
} catch {}

Write-Host ""
Write-Host "$e[$fg;${bg}m  Researchy  -  cream on dark green  $e[0m"
Write-Host "$e[38;2;4;50;34m  dark green text on default bg  $e[0m"
Write-Host ""
