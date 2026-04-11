@echo off
setlocal

if not "%1"=="" (
set silent=%1
) else (
set silent=NULL
)

set workingDir=%CD%

rem echo *******finding ProductType, 1=Work Station, 2= Domain Controller, 3= Server*******
for /f "skip=1 delims=" %%p in ('WMIC Os get ProductType') do if not defined ProductType set ProductType=%%p
if %ProductType%==1 (
  set IsClientOS=true
) else (
  set IsClientOS=false
)
echo ProductType is %ProductType%
echo IsClientOS is equal to %IsClientOS%


rem echo *******finding OS Architecture*******
for /f "skip=1 delims=" %%x in ('WMIC Os get OSArchitecture') do if not defined OSArchitecture set OSArchitecture=%%x
rem echo arch value is %OSArchitecture%
set OSArchitecture=%OSArchitecture:-=~%
for /f "tokens=1 delims=~ " %%i in ('echo %OSArchitecture%') do set OSArchitecture=%%i
rem echo Modified OSArchitecture is %OSArchitecture%
if %OSArchitecture%==64 (
  set OSARCH=x64
) else (
  set OSARCH=x86
)
echo OSARCH is %OSARCH%


rem echo *******finding OS version*******
for /f "tokens=4-5 delims=. " %%i in ('ver') do set OSVERSION=%%i.%%j
if "%IsClientOS%" == "true" (
if "%OSVERSION%" == "5.1" set OSName=wxp
if "%OSVERSION%" == "5.2" set OSName=wxp
if "%OSVERSION%" == "6.0" set OSName=vista
if "%OSVERSION%" == "6.1" set OSName=Win7
if "%OSVERSION%" == "6.2" set OSName=Win8
if "%OSVERSION%" == "6.3" set OSName=Win8.1
if "%OSVERSION%" == "10.0" set OSName=Win10
) else (
if "%OSVERSION%" == "5.2" set OSName=wxp
if "%OSVERSION%" == "6.0" set OSName=vista
if "%OSVERSION%" == "6.1" set OSName=Win7
if "%OSVERSION%" == "6.2" set OSName=Win8
if "%OSVERSION%" == "6.3" set OSName=Win8.1
if "%OSVERSION%" == "10.0" set OSName=Win10
)

echo OS Version is %OSVERSION% and OS Name is %OSName%

set productionFolderPath=%workingDir%\Drivers\%OSName%\%OSARCH%
echo %productionFolderPath%

pushd %productionFolderPath%

rem echo *******Running DPInste.exe*******
if "%silent%"=="/q" (
	"dpinst.exe" /q
 ) else (
	"dpinst.exe" /sw /c
)

popd

set returncode=0
echo  errorlevel is %errorlevel%

rem 0x40000000   = 1073741824 ***Reboot Required***
if %errorlevel% GEQ 1073741824 set returncode=5
	
rem 0x80000000   = 2147483648 ***Driver Package could not be installed***
if %errorlevel% GEQ 2147483648 set returncode=1

echo Driver installation status: %returncode%

EXIT /B %returncode%
@echo on
