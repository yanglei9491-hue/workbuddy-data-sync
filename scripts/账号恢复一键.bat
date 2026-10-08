@echo off
chcp 65001 >nul
title WorkBuddy 数据同步（手动备用入口）
echo ============================================
echo    WorkBuddy 数据同步（手动备用入口）
echo ============================================
echo.
echo  ⚠️ 推荐方式：直接在 WorkBuddy 聊天窗口输入「workbuddy数据同步」，
echo       AI 会自动检测账号、智能推荐，确认后自动迁移并重启，无需手动操作。
echo.
echo  本 bat 是【AI 不可用时】的手动备用入口，需按引导手动操作。
echo  使用前必须先完全退出 WorkBuddy（托盘右键-完全退出），否则脚本会终止。
echo.
echo ============================================
echo.
REM %~dp0 = 本脚本所在目录（scripts），自适应，技能改名/搬家都能跑
cd /d "%~dp0"

REM 通用 Python 启动器：优先 python，其次 py 启动器
where python >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY_EXE=python"
    goto :run
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY_EXE=py -3"
    goto :run
)
set "PY_EXE=python"

:run
"%PY_EXE%" wb_account_restore.py guide
set EXIT_CODE=%ERRORLEVEL%
echo.
echo ============================================
if %EXIT_CODE%==0 (
    echo [完成] 操作结束。
) else (
    echo [未执行] 退出码 %EXIT_CODE%。可能 WorkBuddy 未退出或已取消。
)
echo ============================================
echo.
pause
