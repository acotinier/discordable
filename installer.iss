; Installeur Discordable — compilé par build.py (ISCC /DAppVersion=x.y.z installer.iss)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{174C150E-2B78-4CFB-89A7-E5D52EF49756}
AppName=Discordable
AppVersion={#AppVersion}
AppVerName=Discordable {#AppVersion}
AppPublisher=Alexis
AppPublisherURL=https://github.com/acotinier/discordable
DefaultDirName={autopf}\Discordable
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=build
OutputBaseFilename=Discordable-Setup-{#AppVersion}
SetupIconFile=discordable.ico
UninstallDisplayIcon={app}\Discordable.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "build\dist\Discordable\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Discordable"; Filename: "{app}\Discordable.exe"
Name: "{autodesktop}\Discordable"; Filename: "{app}\Discordable.exe"; Tasks: desktopicon

; Le clic droit est géré par l'appli elle-même (même liste d'extensions que le script)
[Run]
Filename: "{app}\Discordable.exe"; Parameters: "--install"; Flags: runhidden
; Mise à jour depuis l'appli : elle passe /RELAUNCH=<vidéo> pour être rouverte après l'installation
Filename: "{app}\Discordable.exe"; Parameters: """{param:RELAUNCH}"""; Flags: nowait; Check: WantsRelaunch

[UninstallRun]
Filename: "{app}\Discordable.exe"; Parameters: "--uninstall"; Flags: runhidden; RunOnceId: "ContextMenu"

[Code]
function WantsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH}') <> '';
end;
