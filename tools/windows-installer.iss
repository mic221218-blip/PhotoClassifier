#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId=PhotoClassifier
AppName=PhotoClassifier
AppVersion={#AppVersion}
AppPublisher=PhotoClassifier
AppPublisherURL=https://github.com/mic221218-blip/PhotoClassifier
DefaultDirName={localappdata}\Programs\PhotoClassifier
UninstallDisplayIcon={app}\PhotoClassifier.exe
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
SourceDir=..
SetupIconFile=assets\photoclassifier.ico
OutputBaseFilename=PhotoClassifier-Setup-x64
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Files]
Source: ".dsh_work\release\dist\PhotoClassifier\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\PhotoClassifier"; Filename: "{app}\PhotoClassifier.exe"

[Run]
Filename: "{app}\PhotoClassifier.exe"; Flags: nowait postinstall skipifsilent
