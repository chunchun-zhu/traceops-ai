$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败' }
}
& '.\.venv\Scripts\python.exe' -c 'import filelock, langgraph.checkpoint.sqlite, fastapi, langgraph, pymupdf, mcp, pydantic_settings, multipart' 2>$null
if ($LASTEXITCODE -ne 0) {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
}
Write-Host '打开 http://127.0.0.1:8080，按 Ctrl+C 停止。'
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080
