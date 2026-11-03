@echo off
rem Codex 工单队列：按顺序派队列目录（settings.json 的 queueDir）inbox 里的工单，每张一个新会话。
rem 用法：queue.cmd [--once]     停止：在本目录建 STOP 文件；暂停取新工单：在 codex-queue 里建 PAUSE 文件（控制台也能点）
cd /d "%~dp0"
python codex_queue.py %*
