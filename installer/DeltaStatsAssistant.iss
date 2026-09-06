; Per-user installer for the versioned portable application layout.
#ifndef AppVersion
  #error AppVersion must be supplied by the package builder
#endif
#ifndef SourceDir
  #error SourceDir must be supplied by the package builder
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by the package builder
#endif
#ifndef IconFile
  #error IconFile must be supplied by the package builder
#endif

#define AppId "{{F0BB66C0-D6D8-49C2-9C42-8B7CA6AC291B}"
#define AppName "三角洲战绩本"
#define AppPublisher "三角洲战绩本"
; Keep the physical launcher name for in-place updates from older versions.
#define AppExeName "三角洲情报助手.exe"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\DeltaStatsAssistant
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=DeltaStatsAssistant-{#AppVersion}-Setup
SetupIconFile={#IconFile}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
Type: files; Name: "{autoprograms}\三角洲情报助手.lnk"
Type: files; Name: "{autodesktop}\三角洲情报助手.lnk"

[Registry]
Root: HKCU; Subkey: "Software\DeltaStatsAssistant"; ValueType: string; ValueName: "InstalledVersion"; ValueData: "{#AppVersion}"; Flags: uninsdeletevalue

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\.updates"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动三角洲战绩本"; Flags: nowait postinstall skipifsilent

[Code]
function NextVersionPart(const Version: String; var Position: Integer): Integer;
var
  PartStart: Integer;
  PartText: String;
begin
  while (Position <= Length(Version)) and (Version[Position] = '.') do
    Position := Position + 1;
  PartStart := Position;
  while (Position <= Length(Version)) and (Version[Position] >= '0') and (Version[Position] <= '9') do
    Position := Position + 1;
  PartText := Copy(Version, PartStart, Position - PartStart);
  if PartText = '' then
    Result := 0
  else
    Result := StrToIntDef(PartText, 0);
end;

function InstalledVersionIsNewer(const InstalledVersion, CandidateVersion: String): Boolean;
var
  InstalledPosition: Integer;
  CandidatePosition: Integer;
  Index: Integer;
  InstalledPart: Integer;
  CandidatePart: Integer;
begin
  InstalledPosition := 1;
  CandidatePosition := 1;
  for Index := 1 to 4 do
  begin
    InstalledPart := NextVersionPart(InstalledVersion, InstalledPosition);
    CandidatePart := NextVersionPart(CandidateVersion, CandidatePosition);
    if InstalledPart <> CandidatePart then
    begin
      Result := InstalledPart > CandidatePart;
      exit;
    end;
  end;
  Result := False;
end;

function InitializeSetup(): Boolean;
var
  InstalledVersion: String;
begin
  Result := True;
  if RegQueryStringValue(HKCU, 'Software\DeltaStatsAssistant', 'InstalledVersion', InstalledVersion) and
      InstalledVersionIsNewer(InstalledVersion, '{#AppVersion}') then
  begin
    MsgBox(
      '检测到已安装的版本 ' + InstalledVersion + ' 比当前安装包更新。为了避免降级，请下载最新安装包。',
      mbError,
      MB_OK
    );
    Result := False;
  end;
end;
