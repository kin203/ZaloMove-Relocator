$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install pyinstaller
pyinstaller --noconsole --onefile --name ZaloRelocator `
    --icon .\assets\ZaloRelocator.ico `
    --add-data ".\assets\ZaloRelocator.ico;assets" `
    .\src\main.py
Write-Host "Build done: dist\ZaloRelocator.exe"
