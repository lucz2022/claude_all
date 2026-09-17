@echo off
rem Start the MCP data-analysis server on 127.0.0.1:22335
rem Uses the absolute python path so it also works under SYSTEM account (scheduled task / service)
cd /d %~dp0
"C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 22335
