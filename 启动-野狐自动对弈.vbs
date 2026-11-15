' ===================================================================
'  野狐自动对弈 · 静默启动
' -------------------------------------------------------------------
'  双击 .bat 会闪一个黑窗口（命令行窗口），实际用着很碍眼。
'  这里用 WScript 把窗口隐藏掉，只留 GUI。
'  参数 0 = 隐藏窗口，False = 不等待返回。
' ===================================================================
Option Explicit
Dim sh, fso, here, py, args

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

' 优先 py 启动器；找不到就退回裸 python
py = "py -3.14 "
If sh.Run("cmd /c where py >nul 2>nul", 0, True) <> 0 Then
    py = "python "
End If

args = ""
If WScript.Arguments.Count > 0 Then
    Dim i
    For i = 0 To WScript.Arguments.Count - 1
        args = args & " """ & WScript.Arguments(i) & """"
    Next
End If

sh.Run "cmd /c " & py & """" & here & "\gui.py""" & args, 0, False
