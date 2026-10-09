!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Whisper" '"$INSTDIR\whisper-windows-ui.exe"'
  Exec '"$INSTDIR\whisper-windows-ui.exe"'
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; Ensure no running UI holds a lock on the executable before the
  ; uninstall helper runs. taskkill exits non-zero when nothing matches;
  ; NSIS continues regardless.
  ExecWait 'taskkill /F /IM whisper-windows-ui.exe /T'
  ExecWait '"$INSTDIR\whisper-windows-ui.exe" --whisper-uninstall'
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Whisper"
!macroend
