@echo off
rem 把它改成你自己的路径，然后用计划任务每 10 分钟调用一次
cd /d %~dp0..
python collect.py >> data\collect.log 2>&1
python render_html.py >> data\collect.log 2>&1
