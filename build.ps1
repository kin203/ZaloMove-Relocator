$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install pyinstaller
pyinstaller --noconsole --onefile --name ZaloRelocator `
    --optimize 2 `
    --icon .\assets\ZaloRelocator.ico `
    --add-data ".\assets\ZaloRelocator.ico;assets" `
    --exclude-module multiprocessing `
    --exclude-module unittest `
    --exclude-module pydoc `
    .\src\main.py
Write-Host "Build done: dist\ZaloRelocator.exe"
