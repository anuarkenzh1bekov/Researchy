@echo off
REM Researchy palette demo (truecolor ANSI)
REM   cream  #F6E9D9 = 246;233;217
REM   green  #043222 = 4;50;34
REM The <ESC> below is a literal 0x1B byte.
for /f %%a in ('echo prompt $E^| cmd') do set "ESC=%%a"
echo.
echo %ESC%[38;2;246;233;217;48;2;4;50;34m  Researchy  -  cream on dark green  %ESC%[0m
echo %ESC%[38;2;4;50;34m  dark green text on default bg  %ESC%[0m
echo.
