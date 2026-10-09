!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Whisper" '"$INSTDIR\whisper-windows-ui.exe"'
  Exec '"$INSTDIR\whisper-windows-ui.exe"'
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ExecWait '"$INSTDIR\whisper-windows-ui.exe" --whisper-uninstall'
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Whisper"
!macroend
