; Inno Setup recipe for ClipFinder-Setup.exe  (built by .github/workflows/build-windows.yml)
; Installs for the current user only (no administrator rights), so yt-dlp can update itself later.
; Videos and clips are NOT inside the installer and are NOT removed when the program is uninstalled.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{1B2ACEFA-1E6F-4C40-A756-82671EEA26CE}
AppName=ClipFinder
AppVersion={#AppVersion}
AppPublisher=ClipFinder (own tool)
DefaultDirName={autopf}\ClipFinder
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_out
OutputBaseFilename=ClipFinder-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\ClipFinder.exe

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\ClipFinder\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ClipFinder"; Filename: "{app}\ClipFinder.exe"; Parameters: "--data ""{code:DataDir}"""; WorkingDir: "{app}"
Name: "{autodesktop}\ClipFinder"; Filename: "{app}\ClipFinder.exe"; Parameters: "--data ""{code:DataDir}"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipFinder.exe"; Parameters: "--data ""{code:DataDir}"""; WorkingDir: "{app}"; Description: "Start ClipFinder now"; Flags: postinstall nowait skipifsilent

[Code]
var
  DataPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  DataPage := CreateInputDirPage(wpSelectDir,
    'Where should your videos and clips go?',
    'Videos and finished clips can be large (several GB), so pick a drive with free space.',
    'ClipFinder keeps downloaded videos, its analysis and the finished Shorts in this folder. ' +
    'It stays when ClipFinder is uninstalled.',
    False, '');
  DataPage.Add('');
  { not the Documents folder: OneDrive often syncs that, and videos are big }
  if DirExists('D:\Clipfinder') then
    DataPage.Values[0] := GetPreviousData('DataDir', 'D:\Clipfinder\data')
  else
    DataPage.Values[0] := GetPreviousData('DataDir', ExpandConstant('{%USERPROFILE}') + '\ClipFinder');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataPage.Values[0]);
end;

function DataDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(DataPage.Values[0]);
  { a folder path that ends in a backslash would swallow the closing quote of the shortcut }
  if (Length(Result) > 0) and (Result[Length(Result)] = '\') then
    Result := Result + 'ClipFinder';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = DataPage.ID then
    if (Trim(DataPage.Values[0]) = '') or (Pos('"', DataPage.Values[0]) > 0) then
    begin
      MsgBox('Please choose a folder (without quote marks).', mbError, MB_OK);
      Result := False;
    end;
end;
