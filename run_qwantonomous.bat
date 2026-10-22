@echo off
cd /d "C:\opt\servers\qwantonomous"
set PATH=C:\Users\John\AppData\Local\Programs\Python\Python314;C:\Users\John\AppData\Local\Programs\Python\Python314\Scripts;C:\opt\servers;%PATH%
"C:\Users\John\AppData\Local\Programs\Python\Python314\Scripts\uv.exe" run app.py
