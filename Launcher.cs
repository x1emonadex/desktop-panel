using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("Панель")]
[assembly: AssemblyDescription("Панель — быстрый доступ к папкам и ярлыкам")]
[assembly: AssemblyProduct("Панель")]
[assembly: AssemblyCompany("")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Program
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    // Интерпретатор годится, только если в нём есть Pillow
    static bool Works(string exe)
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(exe, "-c \"import PIL\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            using (Process p = Process.Start(psi))
            {
                if (!p.WaitForExit(5000))
                {
                    try { p.Kill(); } catch { }
                    return false;
                }
                return p.ExitCode == 0;
            }
        }
        catch
        {
            return false;
        }
    }

    static void Start(string exe, string args, string dir)
    {
        ProcessStartInfo psi = new ProcessStartInfo(exe, args);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WorkingDirectory = dir;
        Process.Start(psi);
    }

    [STAThread]
    static void Main()
    {
        string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string script = Path.Combine(dir, "panel.pyw");
        if (!File.Exists(script)) return;
        string args = "\"" + script + "\"";

        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidates = new string[]
        {
            "pyw.exe",          // лаунчер Python из PATH
            "pythonw.exe",      // Python из PATH
            Path.Combine(local, @"Programs\Python\Python313\pythonw.exe"),
            Path.Combine(local, @"Programs\Python\Python312\pythonw.exe"),
            Path.Combine(local, @"Programs\Python\Python311\pythonw.exe"),
            @"C:\Program Files\Python313\pythonw.exe",
            @"C:\Program Files\Python312\pythonw.exe",
            @"C:\Program Files\Python311\pythonw.exe",
            @"C:\Program Files\Python310\pythonw.exe"
        };

        foreach (string c in candidates)
        {
            if (Works(c))
            {
                Start(c, args, dir);
                return;
            }
        }

        MessageBoxW(IntPtr.Zero,
            "Не найден Python 3.10+ с библиотекой Pillow.\r\n\r\n" +
            "Установите Python с python.org и выполните в командной строке:\r\n" +
            "    pip install pillow",
            "Панель", 0x10);
    }
}
