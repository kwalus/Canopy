#define AppName "Canopy Tray"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BuildRoot
  #define BuildRoot "."
#endif
#ifndef SourceDir
  #define SourceDir AddBackslash(BuildRoot) + "dist\\Canopy"
#endif

[Setup]
AppId={{7A2378C7-C9ED-4BCE-9BA9-80D5A67F4E9D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Canopy Contributors
DefaultDirName={localappdata}\Canopy Tray
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#BuildRoot}\dist
OutputBaseFilename=CanopyTraySetup-{#AppVersion}
WizardStyle=modern
UninstallDisplayIcon={app}\Canopy.exe
SetupIconFile={#BuildRoot}\canopy_tray\assets\canopy.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Canopy.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Canopy.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Canopy.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
